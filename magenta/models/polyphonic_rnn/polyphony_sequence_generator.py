# Copyright 2016 Google Inc. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""Polyphonic RNN generation code as a SequenceGenerator interface."""

import copy
from functools import partial

# internal imports

import tensorflow as tf

from magenta.models.polyphonic_rnn import polyphony_lib
from magenta.models.polyphonic_rnn import polyphony_model
from magenta.models.polyphonic_rnn.polyphony_lib import PolyphonicEvent

import magenta.music as mm


class PolyphonicRnnSequenceGenerator(mm.BaseSequenceGenerator):
  """Polyphonic RNN generation code as a SequenceGenerator interface."""

  def __init__(self, model, details, steps_per_quarter=4, checkpoint=None,
               bundle=None):
    """Creates a PolyphonicRnnSequenceGenerator.

    Args:
      model: Instance of PolyphonicRnnModel.
      details: A generator_pb2.GeneratorDetails for this generator.
      steps_per_quarter: What precision to use when quantizing the sequence. How
          many steps per quarter note.
      checkpoint: Where to search for the most recent model checkpoint. Mutually
          exclusive with `bundle`.
      bundle: A GeneratorBundle object that includes both the model checkpoint
          and metagraph. Mutually exclusive with `checkpoint`.
    """
    super(PolyphonicRnnSequenceGenerator, self).__init__(
        model, details, steps_per_quarter, checkpoint, bundle)

  def _generate(self, input_sequence, generator_options):
    if len(generator_options.input_sections) > 1:
      raise mm.SequenceGeneratorException(
          'This model supports at most one input_sections message, but got %s' %
          len(generator_options.input_sections))
    if len(generator_options.generate_sections) != 1:
      raise mm.SequenceGeneratorException(
          'This model supports only 1 generate_sections message, but got %s' %
          len(generator_options.generate_sections))

    # This sequence will be quantized later, so it is guaranteed to have only 1
    # tempo.
    qpm = mm.DEFAULT_QUARTERS_PER_MINUTE
    if input_sequence.tempos:
      qpm = input_sequence.tempos[0].qpm

    generate_section = generator_options.generate_sections[0]
    if generator_options.input_sections:
      input_section = generator_options.input_sections[0]
      primer_sequence = mm.extract_subsequence(
          input_sequence, input_section.start_time, input_section.end_time)
      input_start_step = self.seconds_to_steps(
          input_section.start_time, qpm)
    else:
      primer_sequence = input_sequence
      input_start_step = 0

    last_end_time = (max(n.end_time for n in primer_sequence.notes)
                     if primer_sequence.notes else 0)
    if last_end_time > generate_section.start_time:
      raise mm.SequenceGeneratorException(
          'Got GenerateSection request for section that is before or equal to '
          'the end of the NoteSequence. This model can only extend sequences. '
          'Requested start time: %s, Final note end time: %s' %
          (generate_section.start_time, last_end_time))

    # Quantize the priming sequence.
    quantized_primer_sequence = mm.quantize_note_sequence(
        primer_sequence, self.steps_per_quarter)

    extracted_seqs, _ = polyphony_lib.extract_polyphonic_sequences(
        quantized_primer_sequence, start_step=input_start_step)
    assert len(extracted_seqs) <= 1

    generate_start_step = self.seconds_to_steps(
        generate_section.start_time, qpm)
    generate_end_step = self.seconds_to_steps(generate_section.end_time, qpm)

    if extracted_seqs and extracted_seqs[0]:
      poly_seq = extracted_seqs[0]
    else:
      # If no track could be extracted, create an empty track that starts at the
      # requested generate_start_step. This will result in a sequence that
      # contains only the START token.
      poly_seq = polyphony_lib.PolyphonicSequence(
          steps_per_quarter=(
              quantized_primer_sequence.quantization_info.steps_per_quarter),
          start_step=generate_start_step)

    # Ensure that the track extends up to the step we want to start generating.
    poly_seq.set_length(generate_start_step - poly_seq.start_step)
    # Trim any trailing end events to prepare the sequence for more events to be
    # appended during generation.
    poly_seq.trim_trailing_end_events()

    # Extract generation arguments from generator options.
    arg_types = {
        'temperature': lambda arg: arg.float_value,
        'beam_size': lambda arg: arg.int_value,
        'branch_factor': lambda arg: arg.int_value,
        'steps_per_iteration': lambda arg: arg.int_value
    }
    args = dict((name, value_fn(generator_options.args[name]))
                for name, value_fn in arg_types.items()
                if name in generator_options.args)

    # Inject the priming sequence as melody in the output of the generator.
    # Note that start_step is 0 because we overwrite poly_seq below. If we
    # included the priming sequence in poly_seq, it would be poly_seq.num_steps.
    melody_to_inject = copy.deepcopy(poly_seq)
    args['modify_events_callback'] = partial(
        _inject_melody, melody_to_inject, 0)

    # Overwrite poly_seq with a blank sequence to feed into the generator so it
    # is conditioned only on the melody events that are injected as the sequence
    # is created. Otherwise, the generator would have to determine the most
    # likely sequence to follow a monophonic line, which is something not
    # present in the current training data (Bach Chorales).
    poly_seq = polyphony_lib.PolyphonicSequence(
        steps_per_quarter=(
            quantized_primer_sequence.quantization_info.steps_per_quarter),
        start_step=generate_start_step)
    poly_seq.trim_trailing_end_events()

    # If we wanted to include the priming sequence and didn't clear poly_seq
    # above, this is how we would calculate total_steps.
    # total_steps = poly_seq.num_steps + (
    #     generate_end_step - generate_start_step)

    total_steps = generate_end_step - generate_start_step

    while poly_seq.num_steps < total_steps:
      # Assume it takes ~5 rnn steps to generate one quantized step.
      # Can't know for sure until generation is finished because the number of
      # notes per quantized step is variable.
      steps_to_gen = total_steps - poly_seq.num_steps
      rnn_steps_to_gen = 5 * steps_to_gen
      tf.logging.info(
          'Need to generate %d more steps for this sequence, will try asking '
          'for %d RNN steps' % (steps_to_gen, rnn_steps_to_gen))
      poly_seq = self._model.generate_polyphonic_sequence(
          len(poly_seq) + rnn_steps_to_gen, poly_seq, **args)
    poly_seq.set_length(total_steps)

    # Specify a base_note_sequence because the priming sequence is not included
    # in poly_seq. If we did not clear poly_seq above, then we would not want to
    # specify a base_note_sequence.
    generated_sequence = poly_seq.to_sequence(
        qpm=qpm, base_note_sequence=copy.deepcopy(primer_sequence))
    assert (generated_sequence.total_time - generate_section.end_time) <= 1e-5
    return generated_sequence


def _inject_melody(melody, start_step, encoder_decoder, event_sequences,
                   inputs):
  """A modify_events_callback method for generate_polyphonic_sequence.

  Should be called with functools.partial first, to fill in the melody and
  start_step arguments.

  Will extend the event sequence using events from the melody argument whenever
  the event sequence gets to a new step.

  Args:
    melody: The PolyphonicSequence to use to extend the event sequence.
    start_step: The length of the priming sequence in RNN steps.
    encoder_decoder: Supplied by the callback. The current
        EventSequenceEncoderDecoder.
    event_sequences: Supplied by the callback. The current EventSequence.
    inputs: Supplied by the callback. The current list of encoded events.
  """
  assert len(event_sequences) == len(inputs)

  for i in range(len(inputs)):
    event_sequence = event_sequences[i]
    input_ = inputs[i]

    # Only modify the event sequence if we're at the start of a new step or this
    # is the first step.
    if not (event_sequence[-1].event_type == PolyphonicEvent.STEP_END or
            not event_sequence or
            (event_sequence[-1].event_type == PolyphonicEvent.START and
             len(event_sequence) == 1)):
      continue

    # Determine the current step event.
    event_step_count = 0
    for event in event_sequence:
      if event.event_type == PolyphonicEvent.STEP_END:
        event_step_count += 1

    # Find the corresponding event in the input melody.
    melody_step_count = start_step
    for i, event in enumerate(melody):
      if event.event_type == PolyphonicEvent.STEP_END:
        melody_step_count += 1
      if melody_step_count == event_step_count:
        melody_pos = i + 1
        while melody_pos < len(melody) and (
            melody[melody_pos].event_type != PolyphonicEvent.STEP_END):
          event_sequence.append(melody[melody_pos])
          input_.extend(encoder_decoder.get_inputs_batch([event_sequence])[0])
          melody_pos += 1
        break


def get_generator_map():
  """Returns a map from the generator ID to a SequenceGenerator class creator.

  Binds the `config` argument so that the arguments match the
  BaseSequenceGenerator class constructor.

  Returns:
    Map from the generator ID to its SequenceGenerator class creator with a
    bound `config` argument.
  """
  def create_sequence_generator(config, **kwargs):
    return PolyphonicRnnSequenceGenerator(
        polyphony_model.PolyphonicRnnModel(config), config.details, **kwargs)

  return {key: partial(create_sequence_generator, config)
          for (key, config) in polyphony_model.default_configs.items()}