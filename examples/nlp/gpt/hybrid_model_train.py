# Copyright (c) 2023, NVIDIA CORPORATION.  All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import os
import re
from collections import defaultdict
from copy import deepcopy
from dataclasses import dataclass
from functools import partial
from itertools import chain
from pathlib import Path

import torch
import torch.multiprocessing as mp
from datasets import load_dataset
from megatron.core import parallel_state
from megatron.core.utils import divide
from nemo_skills.code_execution.math_grader import extract_answer
from omegaconf import open_dict
from omegaconf.omegaconf import OmegaConf

from nemo.core.config import hydra_runner
from nemo.utils import logging
from nemo.utils.exp_manager import exp_manager
from nemo_aligner.algorithms.deepsearch import DeepSearchTrainer
from nemo_aligner.data.nlp.builders import build_dataloader
from nemo_aligner.data.nlp.datasets import LLaMa3ChatDataset
from nemo_aligner.models.nlp.gpt.megatron_gpt_hybrid_model import MegatronGPTHybridModel
from nemo_aligner.utils.customized_nlpdpstrategy import CustomMegatronTrainerBuilder
from nemo_aligner.utils.deep_search.mcts.feedback_functions import GSK8KFeedbackDataset, MathSandBoxedFeedBack
from nemo_aligner.utils.distributed import Timer
from nemo_aligner.utils.train_script_utils import (
    CustomLoggerWrapper,
    FakeScheduler,
    add_custom_checkpoint_callback,
    extract_optimizer_scheduler_from_ptl_model,
    init_distributed,
    init_using_ptl,
    resolve_and_create_trainer,
    retrieve_custom_trainer_state_dict,
    temp_pop_from_config,
)
from nemo_aligner.utils.trainer_utils import compute_limit_batches
from nemo_aligner.utils.utils import load_and_override_model_config, load_from_nemo

"""Script to start Reward Model training"""

OmegaConf.register_new_resolver("multiply", lambda x, y: x * y, replace=True)
OmegaConf.register_new_resolver("int_div", lambda x, y: x // y, replace=True)
OmegaConf.register_new_resolver("not", lambda x: not x)

mp.set_start_method("spawn", force=True)


class DatasetLoader:
    def __init__(self, dictionary):
        # no need to shuffle it because the dataloader does
        paths = list(sorted(chain.from_iterable(dictionary.values())))
        self.paths = paths

    def __getitem__(self, idx):
        return torch.load(self.paths[idx])

    def __len__(self):
        return len(self.paths)


def process_data_id(item):
    output = item.pop("data_id").split("@")

    if len(output) == 1:
        data_id = int(output[0])
        replica_id = 0
    else:
        data_id, replica_id = map(int, output)

    item["data_id"] = data_id
    item["replica_id"] = replica_id
    return item


def parse_filename(filename, start_string):
    # Define the regex pattern to capture data ID and replica ID
    pattern = start_string + r"_(\d+)@(\d+)"

    # Search for the pattern in the filename
    match = re.search(pattern, filename)

    if match:
        # Extract the data ID and replica ID from the matched groups
        data_id = match.group(1)
        replica_id = match.group(2)
        return int(data_id), int(replica_id)
    else:
        # Return None if no match is found
        return None


def get_paths(path_or_dir):
    path = Path(path_or_dir)
    assert path.exists()

    policy_paths = sorted(path.glob("policy_data*.pt"))
    value_paths = sorted(path.glob("value_data*.pt"))

    policy_data_ids = defaultdict(list)
    value_data_ids = defaultdict(list)

    for p in policy_paths:
        data_id, replica_id = parse_filename(p.name, start_string="policy_data")
        policy_data_ids[data_id].append(p)

    for p in value_paths:
        data_id, replica_id = parse_filename(p.name, start_string="value_data")
        value_data_ids[data_id].append(p)

    return policy_data_ids, value_data_ids


def collate_fn(batch):
    # applies the steerlm format and
    # transposes the list of dict to dict of lists
    new_dict = defaultdict(list)

    for b in batch:
        new_dict["question"].append(b["question"])
        new_dict["answer"].append(b["expected_answer"])

    return new_dict


def fill_padded_tensor_with_data(batches, max_seqlen, lengths, response_lengths, pad_value):
    """unpadded x * -> B x max seq len x *"""
    assert len(batches) == len(lengths)

    output = batches[0].new_empty((len(batches), max_seqlen, *batches[0].shape[1:])).fill_(pad_value)

    for i, (batch, length, response_length) in enumerate(zip(batches, lengths, response_lengths, strict=True)):
        idx = length - 1
        output[i, idx:response_length, ...] = batch

    return output


def create_mask(tokens, lengths, response_length, actions=None):
    idx = lengths - 1

    end = (response_length).view(-1, 1)
    start = idx.view(-1, 1)

    seq_range = torch.arange(tokens.size(-1), device=lengths.device).view(1, -1)
    sequence_mask = (start <= seq_range) & (end > seq_range)

    if actions is not None:
        sequence_mask = sequence_mask[..., None] & (actions != -1)

    return sequence_mask


def mcts_collate_fn(eos_id, batch):
    new_dict = {}
    context_keys = {"context_length", "response_length"}
    token_keys = {"tokens"}
    fill_keys = {"action_probs", "reward", "actions"}

    max_seqlen = max(len(x["tokens"]) for x in batch)
    lengths = [x["context_length"] for x in batch]

    for k in context_keys | token_keys:
        batches = tuple(torch.as_tensor(x[k]) for x in batch)

        if k in context_keys:
            output = torch.stack(batches)
        elif k in token_keys:
            output = torch.nn.utils.rnn.pad_sequence(batches, batch_first=True, padding_value=eos_id,)

        new_dict[k] = output

    max_seqlen = new_dict["tokens"].size(-1)
    lengths = new_dict["context_length"]
    response_length = new_dict["response_length"]

    for k in fill_keys:
        output = fill_padded_tensor_with_data(
            tuple(torch.as_tensor(x[k]) for x in batch), max_seqlen, lengths, response_length, 0
        )
        new_dict[k] = output

    mask = create_mask(new_dict["tokens"], lengths, new_dict["response_length"], new_dict["actions"])

    # after masking the actions need to be 0ed otherwise it crashes the training code
    new_dict["actions"].clamp_(min=0)
    return new_dict | {"mcts_mask": mask}


def mcts_value_collate_fn(eos_id, batches):
    new_dict = defaultdict(list)

    for batch in batches:
        new_dict["tokens"].extend(batch["tokens"])
        new_dict["token_values"].extend(batch["token_values"])
        new_dict["reward"].extend(batch["reward"])
        new_dict["response_length"].extend(list(len(x) for x in batch["tokens"]))
        new_dict["context_length"].extend([batch["context_length"]] * len(batch["tokens"]))

    final_dict = {}
    for k, v in new_dict.items():
        if k == "tokens":
            inputs = tuple(torch.as_tensor(x) for x in v)
            output = torch.nn.utils.rnn.pad_sequence(inputs, batch_first=True, padding_value=eos_id,)
        elif k == "token_values":
            inputs = tuple(torch.as_tensor(x) for x in v)
            output = torch.nn.utils.rnn.pad_sequence(inputs, batch_first=True, padding_value=0)
        else:
            output = torch.as_tensor(v)

        final_dict[k] = output

    # TODO: do i need to add masking for this?
    mask = create_mask(final_dict["tokens"], final_dict["context_length"], final_dict["response_length"])

    return final_dict | {"mcts_mask": mask}


@hydra_runner(config_path="conf", config_name="gpt_hybrid_train")
def main(cfg) -> None:

    feedback = MathSandBoxedFeedBack(
        host=os.getenv("NEMO_SKILLS_SANDBOX_HOST"), port=os.getenv("NEMO_SKILLS_SANDBOX_PORT")
    )

    train_policy_data_paths, train_value_data_paths = get_paths(cfg.mcts_data_file)
    val_policy_data_paths, val_value_data_paths = get_paths(cfg.mcts_data_file_val)

    cfg.model = load_and_override_model_config(cfg.pretrained_checkpoint.restore_from_path, cfg.model)
    cfg.model.value = load_and_override_model_config(cfg.pretrained_checkpoint.restore_from_path, cfg.model.value)

    logging.info("\n\n************** Experiment configuration ***********")
    logging.info(f"\n{OmegaConf.to_yaml(cfg)}")

    trainer = resolve_and_create_trainer(cfg, "deep_search")

    exp_manager(trainer, cfg.exp_manager)
    logger = CustomLoggerWrapper(trainer.loggers)

    hybrid_model_cls = MegatronGPTHybridModel

    ptl_model = load_from_nemo(
        hybrid_model_cls,
        cfg.model,
        trainer,
        strict=True,
        load_base_model_only=not cfg.pretrained_checkpoint.from_mcts_trained,
        restore_path=cfg.pretrained_checkpoint.restore_from_path,
    )

    tokenizer = ptl_model.tokenizer

    train_ds = LLaMa3ChatDataset(cfg.dataset.data_prefix["train"], cfg.dataset.prompt_template_name, tokenizer)
    val_ds = LLaMa3ChatDataset(cfg.dataset.data_prefix["validation"], cfg.dataset.prompt_template_name, tokenizer)

    trainer_restore_path = trainer.ckpt_path
    if trainer_restore_path is not None:
        custom_trainer_state_dict = retrieve_custom_trainer_state_dict(trainer)
        consumed_samples = custom_trainer_state_dict["consumed_samples"]
        consumed_samples_values = custom_trainer_state_dict["consumed_samples_values"]
    else:
        custom_trainer_state_dict = None
        consumed_samples = 0
        consumed_samples_values = 0

    init_distributed(trainer, ptl_model, cfg.model.get("transformer_engine", False))

    dp_size = parallel_state.get_data_parallel_world_size()

    eos_id = ptl_model.tokenizer.eos_id

    # TODO(geshen): consumed samples need to be different for each of these 2 dataloaders
    # TODO(geshen): support multiple epochs
    train_policy_dataloader = build_dataloader(
        cfg=cfg,
        dataset=DatasetLoader(train_policy_data_paths),
        consumed_samples=consumed_samples,
        mbs=cfg.model.micro_batch_size,
        gbs=cfg.model.global_batch_size,
        load_gbs=True,
        collate_fn=partial(mcts_collate_fn, eos_id),
        shuffle=True,
    )

    # TODO(geshen): can have different mbs
    train_value_dataloader = build_dataloader(
        cfg=cfg,
        dataset=DatasetLoader(train_value_data_paths),
        consumed_samples=consumed_samples_values,
        mbs=cfg.model.micro_batch_size,
        gbs=cfg.model.critic_global_batch_size,
        load_gbs=True,
        collate_fn=partial(mcts_value_collate_fn, eos_id),
        shuffle=True,
    )

    # hack to allow using all of the validation dataset
    # TODO: partial this dataloader into the func
    val_dataloader_builder_func = partial(
        build_dataloader,
        cfg=cfg,
        dataset=val_ds,
        consumed_samples=0,
        mbs=cfg.model.inference.micro_batch_size,
        gbs=cfg.model.inference.micro_batch_size * dp_size,
        load_gbs=False,
        collate_fn=collate_fn,
        drop_last=False,
        shuffle=False,
    )

    train_dataloader_builder_func = partial(
        build_dataloader,
        cfg=cfg,
        dataset=train_ds,
        consumed_samples=0,
        mbs=cfg.model.inference.micro_batch_size,
        gbs=cfg.model.inference.micro_batch_size * dp_size,
        load_gbs=False,
        collate_fn=collate_fn,
        drop_last=True,
        shuffle=False,
    )

    assert cfg.trainer.deep_search.max_epochs > 0

    val_policy_dataloader = build_dataloader(
        cfg=cfg,
        dataset=DatasetLoader(val_policy_data_paths),
        consumed_samples=consumed_samples,
        mbs=cfg.model.micro_batch_size,
        gbs=cfg.model.global_batch_size,
        load_gbs=True,
        collate_fn=partial(mcts_collate_fn, eos_id),
        shuffle=True,
    )

    # TODO(geshen): can have different mbs
    val_value_dataloader = build_dataloader(
        cfg=cfg,
        dataset=DatasetLoader(val_value_data_paths),
        consumed_samples=consumed_samples_values,
        mbs=cfg.model.micro_batch_size,
        gbs=cfg.model.critic_global_batch_size,
        load_gbs=True,
        collate_fn=partial(mcts_value_collate_fn, eos_id),
        shuffle=True,
    )

    # on the first time we ever save a checkpoint
    # these steps will be set correctly and subsequent resumes
    # we rely on PTL keeping the max step in the state dict
    # to set it properly, since the below would be incorrect
    def set_max_steps(sched, steps):
        if sched is not None and "max_steps" not in sched:
            with open_dict(sched):
                sched.max_steps = steps

    policy_steps = len(train_policy_dataloader) * cfg.trainer.deep_search.max_epochs
    value_steps = len(train_value_dataloader) * cfg.trainer.deep_search.max_epochs

    set_max_steps(ptl_model.cfg.optim.get("sched", None), max(policy_steps, value_steps))

    init_using_ptl(trainer, ptl_model, None, None)

    optimizer, scheduler = extract_optimizer_scheduler_from_ptl_model(ptl_model)

    ckpt_callback = add_custom_checkpoint_callback(trainer, ptl_model)

    logger.log_hyperparams(OmegaConf.to_container(cfg))
    timer = Timer(cfg.exp_manager.get("max_time_per_run"))

    deep_search_trainer = DeepSearchTrainer(
        cfg=cfg.trainer.deep_search,
        model=ptl_model,
        optimizer=optimizer,
        scheduler=scheduler,
        train_policy_dataloader=train_policy_dataloader,
        train_value_dataloader=train_value_dataloader,
        val_policy_dataloader=val_policy_dataloader,
        val_value_dataloader=val_value_dataloader,
        val_dataloader_builder_func=val_dataloader_builder_func,
        train_dataloader_builder_func=train_dataloader_builder_func,
        feedback=feedback,
        logger=logger,
        ckpt_callback=ckpt_callback,
        run_timer=timer,
    )

    if custom_trainer_state_dict is not None:
        deep_search_trainer.load_state_dict(custom_trainer_state_dict)

    deep_search_trainer.fit()


if __name__ == "__main__":
    main()
