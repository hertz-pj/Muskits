import argparse
import logging
from typing import Callable
from typing import Collection
from typing import Dict
from typing import List
from typing import Optional
from typing import Tuple

import numpy as np
import torch
from typeguard import check_argument_types
from typeguard import check_return_type

from muskit.layers.abs_normalize import AbsNormalize
from muskit.layers.global_mvn import GlobalMVN
from muskit.tasks.abs_task import AbsTask
from muskit.train.class_choices import ClassChoices
from muskit.train.collate_fn import CommonCollateFn
from muskit.train.preprocessor import CommonPreprocessor
from muskit.train.trainer import Trainer
from muskit.svs.abs_svs import AbsSVS
from muskit.svs.muskit_model import MuskitSVSModel
from muskit.svs.feats_extract.abs_feats_extract import AbsFeatsExtract
from muskit.svs.feats_extract.dio import Dio
from muskit.svs.feats_extract.score_feats_extract import FrameScoreFeats
from muskit.svs.feats_extract.score_feats_extract import SyllableScoreFeats
from muskit.svs.feats_extract.energy import Energy
from muskit.svs.feats_extract.log_mel_fbank import LogMelFbank
from muskit.svs.feats_extract.log_spectrogram import LogSpectrogram
from muskit.svs.encoder_decoder.transformer.transformer import Transformer
from muskit.svs.bytesing.bytesing import ByteSing
from muskit.svs.naive_rnn.naive_rnn import NaiveRNN
from muskit.svs.mlp_singer.mlp_singer import MLPSinger
from muskit.svs.glu_transformer.glu_transformer import GLU_Transformer
from muskit.svs.xiaoice.XiaoiceSing import XiaoiceSing
from muskit.svs.xiaoice.XiaoiceSing import XiaoiceSing_noDP
from muskit.utils.get_default_kwargs import get_default_kwargs
from muskit.utils.nested_dict_action import NestedDictAction
from muskit.utils.types import int_or_none
from muskit.utils.types import str2bool
from muskit.utils.types import str_or_none

feats_extractor_choices = ClassChoices(
    "feats_extract",
    classes=dict(fbank=LogMelFbank, spectrogram=LogSpectrogram),
    type_check=AbsFeatsExtract,
    default="fbank",
)

score_feats_extractor_choices = ClassChoices(
    "score_feats_extract",
    classes=dict(
        frame_score_feats=FrameScoreFeats, syllable_score_feats=SyllableScoreFeats
    ),
    type_check=AbsFeatsExtract,
    default="frame_score_feats",
)

pitch_extractor_choices = ClassChoices(
    "pitch_extract",
    classes=dict(dio=Dio),
    type_check=AbsFeatsExtract,
    default=None,
    optional=True,
)
energy_extractor_choices = ClassChoices(
    "energy_extract",
    classes=dict(energy=Energy),
    type_check=AbsFeatsExtract,
    default=None,
    optional=True,
)
normalize_choices = ClassChoices(
    "normalize",
    classes=dict(global_mvn=GlobalMVN),
    type_check=AbsNormalize,
    default="global_mvn",
    optional=True,
)
pitch_normalize_choices = ClassChoices(
    "pitch_normalize",
    classes=dict(global_mvn=GlobalMVN),
    type_check=AbsNormalize,
    default=None,
    optional=True,
)
energy_normalize_choices = ClassChoices(
    "energy_normalize",
    classes=dict(global_mvn=GlobalMVN),
    type_check=AbsNormalize,
    default=None,
    optional=True,
)
svs_choices = ClassChoices(
    "svs",
    classes=dict(
        transformer=Transformer,
        glu_transformer=GLU_Transformer,
        bytesing=ByteSing,
        naive_rnn=NaiveRNN,
        mlp=MLPSinger,
        xiaoice=XiaoiceSing,
        xiaoice_noDP=XiaoiceSing_noDP,
    ),
    type_check=AbsSVS,
    default="transformer",
)


class SVSTask(AbsTask):
    num_optimizers: int = 1

    # Add variable objects configurations
    class_choices_list = [
        # --score_extractor and --score_extractor_conf
        score_feats_extractor_choices,
        # --feats_extractor and --feats_extractor_conf
        feats_extractor_choices,
        # --normalize and --normalize_conf
        normalize_choices,
        # --svs and --svs_conf
        svs_choices,
        # --pitch_extract and --pitch_extract_conf
        pitch_extractor_choices,
        # --pitch_normalize and --pitch_normalize_conf
        pitch_normalize_choices,
        # --energy_extract and --energy_extract_conf
        energy_extractor_choices,
        # --energy_normalize and --energy_normalize_conf
        energy_normalize_choices,
    ]

    # If you need to modify train() or eval() procedures, change Trainer class here
    trainer = Trainer

    @classmethod
    def add_task_arguments(cls, parser: argparse.ArgumentParser):
        # NOTE(kamo): Use '_' instead of '-' to avoid confusion
        assert check_argument_types()
        group = parser.add_argument_group(description="Task related")

        # NOTE(kamo): add_arguments(..., required=True) can't be used
        # to provide --print_config mode. Instead of it, do as
        required = parser.get_default("required")
        required += ["token_list"]

        group.add_argument(
            "--token_list",
            type=str_or_none,
            default=None,
            help="A text mapping int-id to token",
        )
        group.add_argument(
            "--odim",
            type=int_or_none,
            default=None,
            help="The number of dimension of output feature",
        )
        group.add_argument(
            "--model_conf",
            action=NestedDictAction,
            default=get_default_kwargs(MuskitSVSModel),
            help="The keyword arguments for model class.",
        )

        group = parser.add_argument_group(description="Preprocess related")
        group.add_argument(
            "--use_preprocessor",
            type=str2bool,
            default=True,
            help="Apply preprocessing to data or not",
        )
        group.add_argument(
            "--token_type",
            type=str,
            default="phn",
            choices=["bpe", "char", "word", "phn"],
            help="The text will be tokenized in the specified level token",
        )
        group.add_argument(
            "--bpemodel",
            type=str_or_none,
            default=None,
            help="The model file of sentencepiece",
        )
        parser.add_argument(
            "--non_linguistic_symbols",
            type=str_or_none,
            help="non_linguistic_symbols file path",
        )
        parser.add_argument(
            "--cleaner",
            type=str_or_none,
            choices=[None, "tacotron", "jaconv", "vietnamese"],
            default=None,
            help="Apply text cleaning",
        )
        parser.add_argument(
            "--g2p",
            type=str_or_none,
            choices=[
                None,
                "g2p_en",
                "g2p_en_no_space",
                "pyopenjtalk",
                "pyopenjtalk_kana",
                "pyopenjtalk_accent",
                "pyopenjtalk_accent_with_pause",
                "pypinyin_g2p",
                "pypinyin_g2p_phone",
                "espeak_ng_arabic",
            ],
            default=None,
            help="Specify g2p method if --token_type=phn",
        )

        parser.add_argument(
            "--fs",
            type=int,
            default=24000,  # BUG: another fs in feats_extract_conf
            help="sample rate",
        )

        for class_choices in cls.class_choices_list:
            # Append --<name> and --<name>_conf.
            # e.g. --encoder and --encoder_conf
            class_choices.add_arguments(group)

    @classmethod
    def build_collate_fn(
        cls, args: argparse.Namespace, train: bool
    ) -> Callable[
        [Collection[Tuple[str, Dict[str, np.ndarray]]]],
        Tuple[List[str], Dict[str, torch.Tensor]],
    ]:
        assert check_argument_types()
        return CommonCollateFn(
            float_pad_value=0.0, int_pad_value=0, not_sequence=["spembs"]
        )

    @classmethod
    def build_preprocess_fn(
        cls, args: argparse.Namespace, train: bool
    ) -> Optional[Callable[[str, Dict[str, np.array], float], Dict[str, np.ndarray]]]:
        assert check_argument_types()
        if args.use_preprocessor:
            retval = CommonPreprocessor(
                train=train,
                token_type=args.token_type,
                token_list=args.token_list,
                bpemodel=args.bpemodel,
                non_linguistic_symbols=args.non_linguistic_symbols,
                text_cleaner=args.cleaner,
                g2p_type=args.g2p,
                fs=args.fs,
            )
        else:
            retval = None
        assert check_return_type(retval)
        return retval

    @classmethod
    def required_data_names(
        cls, train: bool = True, inference: bool = False
    ) -> Tuple[str, ...]:
        if not inference:
            retval = ("text", "singing", "midi", "label")
        else:
            # Inference mode
            retval = ("text", "midi", "label")
        return retval

    @classmethod
    def optional_data_names(
        cls, train: bool = True, inference: bool = False
    ) -> Tuple[str, ...]:
        if not inference:
            retval = ("spembs", "durations", "pitch", "energy")
        else:
            # Inference mode
            retval = ("spembs", "singing", "durations")
        return retval

    @classmethod
    def build_model(cls, args: argparse.Namespace) -> MuskitSVSModel:
        assert check_argument_types()
        if isinstance(args.token_list, str):
            with open(args.token_list, encoding="utf-8") as f:
                token_list = [line.rstrip() for line in f]

            # "args" is saved as it is in a yaml file by BaseTask.main().
            # Overwriting token_list to keep it as "portable".
            args.token_list = token_list.copy()
        elif isinstance(args.token_list, (tuple, list)):
            token_list = args.token_list.copy()
        else:
            raise RuntimeError("token_list must be str or dict")

        vocab_size = len(token_list)
        logging.info(f"Vocabulary size: {vocab_size }")

        # 1. feats_extract
        if args.odim is None:
            # Extract features in the model
            feats_extract_class = feats_extractor_choices.get_class(args.feats_extract)
            feats_extract = feats_extract_class(**args.feats_extract_conf)
            odim = feats_extract.output_size()
        else:
            # Give features from data-loader
            args.feats_extract = None
            args.feats_extract_conf = None
            feats_extract = None
            odim = args.odim

        # 2. Normalization layer
        if args.normalize is not None:
            normalize_class = normalize_choices.get_class(args.normalize)
            normalize = normalize_class(**args.normalize_conf)
        else:
            normalize = None

        # 3. SVS
        svs_class = svs_choices.get_class(args.svs)
        svs = svs_class(idim=vocab_size, odim=odim, **args.svs_conf)

        # 4. Extra components
        score_feats_extract = None
        pitch_extract = None
        energy_extract = None
        pitch_normalize = None
        energy_normalize = None
        logging.info(f"args:{args}")
        if getattr(args, "score_feats_extract", None) is not None:
            score_feats_extract_class = score_feats_extractor_choices.get_class(
                args.score_feats_extract
            )
            score_feats_extract = score_feats_extract_class(
                **args.score_feats_extract_conf
            )
        if getattr(args, "pitch_extract", None) is not None:
            pitch_extract_class = pitch_extractor_choices.get_class(args.pitch_extract)
            if args.pitch_extract_conf.get("reduction_factor", None) is not None:
                assert args.pitch_extract_conf.get(
                    "reduction_factor", None
                ) == args.svs_conf.get("reduction_factor", 1)
            else:
                args.pitch_extract_conf["reduction_factor"] = args.svs_conf.get(
                    "reduction_factor", 1
                )
            pitch_extract = pitch_extract_class(**args.pitch_extract_conf)
        # logging.info(f'pitch_extract:{pitch_extract}')
        if getattr(args, "energy_extract", None) is not None:
            if args.energy_extract_conf.get("reduction_factor", None) is not None:
                assert args.energy_extract_conf.get(
                    "reduction_factor", None
                ) == args.svs_conf.get("reduction_factor", 1)
            else:
                args.energy_extract_conf["reduction_factor"] = args.svs_conf.get(
                    "reduction_factor", 1
                )
            energy_extract_class = energy_extractor_choices.get_class(
                args.energy_extract
            )
            energy_extract = energy_extract_class(**args.energy_extract_conf)
        if getattr(args, "pitch_normalize", None) is not None:
            pitch_normalize_class = pitch_normalize_choices.get_class(
                args.pitch_normalize
            )
            pitch_normalize = pitch_normalize_class(**args.pitch_normalize_conf)
        if getattr(args, "energy_normalize", None) is not None:
            energy_normalize_class = energy_normalize_choices.get_class(
                args.energy_normalize
            )
            energy_normalize = energy_normalize_class(**args.energy_normalize_conf)

        # 5. Build model
        model = MuskitSVSModel(
            text_extract=score_feats_extract,
            feats_extract=feats_extract,
            score_feats_extract=score_feats_extract,
            durations_extract=score_feats_extract,
            pitch_extract=pitch_extract,
            tempo_extract=score_feats_extract,
            energy_extract=energy_extract,
            normalize=normalize,
            pitch_normalize=pitch_normalize,
            energy_normalize=energy_normalize,
            svs=svs,
            **args.model_conf,
        )
        assert check_return_type(model)
        return model

    # @classmethod
