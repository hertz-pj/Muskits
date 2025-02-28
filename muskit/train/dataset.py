from abc import ABC
from abc import abstractmethod
import collections
import copy
import functools
import logging
import numbers
import random
import re
from typing import Any
from typing import Callable
from typing import Collection
from typing import Dict
from typing import Mapping
from typing import Tuple
from typing import Union

import h5py
import humanfriendly
import kaldiio
import numpy as np
import torch
from torch.utils.data.dataset import Dataset
from typeguard import check_argument_types
from typeguard import check_return_type

from muskit.fileio.npy_scp import NpyScpReader
from muskit.fileio.rand_gen_dataset import FloatRandomGenerateDataset
from muskit.fileio.rand_gen_dataset import IntRandomGenerateDataset
from muskit.fileio.read_text import load_num_sequence_text
from muskit.fileio.read_text import read_2column_text
from muskit.fileio.read_text import read_label
from muskit.fileio.sound_scp import SoundScpReader
from muskit.fileio.midi_scp import MIDIScpReader
from muskit.utils.sized_dict import SizedDict


class AdapterForSoundScpReader(collections.abc.Mapping):
    def __init__(self, loader, dtype=None):
        assert check_argument_types()
        self.loader = loader
        self.dtype = dtype
        self.rate = None

    def keys(self):
        return self.loader.keys()

    def __len__(self):
        return len(self.loader)

    def __iter__(self):
        return iter(self.loader)

    def __getitem__(self, key: (str, int)) -> np.ndarray:
        key, pitch_aug_factor, time_aug_factor = key
        retval = self.loader[(key, pitch_aug_factor, time_aug_factor)]

        if isinstance(retval, tuple):
            assert len(retval) == 2, len(retval)
            if isinstance(retval[0], int) and isinstance(retval[1], np.ndarray):
                # sound scp case
                rate, array = retval
            elif isinstance(retval[0], int) and isinstance(retval[1], np.ndarray):
                # Extended ark format case
                array, rate = retval
            # elif isinstance(retval[0], np.ndarray) and isinstance(retval[1], np.ndarray):
            #     # Extended ark format case
            #     array, rate = retval
            else:
                raise RuntimeError(
                    f"Unexpected type: {type(retval[0])}, {type(retval[1])}"
                )

            if self.rate is not None and self.rate != rate:
                raise RuntimeError(
                    f"Sampling rates are mismatched: {self.rate} != {rate}"
                )
            self.rate = rate
            # Multichannel wave fie
            # array: (NSample, Channel) or (Nsample)
            if self.dtype is not None:
                array = array.astype(self.dtype)

        elif isinstance(retval, list):
            # label: [ [start, end, phone] * n ]
            array = retval
        else:
            # Normal ark case
            assert isinstance(retval, np.ndarray), type(retval)
            array = retval
            if self.dtype is not None:
                array = array.astype(self.dtype)

        assert isinstance(array, np.ndarray) or isinstance(array, list), type(array)
        return array


class H5FileWrapper:
    def __init__(self, path: str):
        self.path = path
        self.h5_file = h5py.File(path, "r")

    def __repr__(self) -> str:
        return str(self.h5_file)

    def __len__(self) -> int:
        return len(self.h5_file)

    def __iter__(self):
        return iter(self.h5_file)

    def __getitem__(self, key) -> np.ndarray:
        value = self.h5_file[key]
        return value[()]


class AdapterForMIDIScpReader(collections.abc.Mapping):
    def __init__(self, loader):
        assert check_argument_types()
        self.loader = loader

    def keys(self):
        return self.loader.keys()

    def __len__(self):
        return len(self.loader)

    def __iter__(self):
        return iter(self.loader)

    def __getitem__(self, key: (str, int)) -> np.ndarray:
        key, pitch_aug_factor, time_aug_factor = key
        retval = self.loader[(key, pitch_aug_factor, time_aug_factor)]

        assert len(retval) == 2, len(retval)
        if isinstance(retval[0], np.ndarray) and isinstance(retval[1], np.ndarray):
            note_array, tempo_array = retval
        else:
            raise RuntimeError(f"Unexpected type: {type(retval[0])}, {type(retval[1])}")

        assert isinstance(note_array, np.ndarray) and isinstance(
            tempo_array, np.ndarray
        )
        return note_array, tempo_array


class AdapterForLabelScpReader(collections.abc.Mapping):
    def __init__(self, loader):
        assert check_argument_types()
        self.loader = loader

    def keys(self):
        return self.loader.keys()

    def __len__(self):
        return len(self.loader)

    def __iter__(self):
        return iter(self.loader)

    def __getitem__(self, key: str) -> np.ndarray:
        retval = self.loader[key]

        assert isinstance(retval, list)
        seq_len = len(retval)
        sample_time = np.zeros((seq_len, 2))
        sample_label = []
        for i in range(seq_len):
            sample_time[i, 0] = np.float32(retval[i][0])
            sample_time[i, 1] = np.float32(retval[i][1])
            sample_label.append(retval[i][2])

        assert isinstance(sample_time, np.ndarray) and isinstance(sample_label, list)
        return sample_time, sample_label


def sound_loader(path, float_dtype=None):
    # The file is as follows:
    #   utterance_id_A /some/where/a.wav
    #   utterance_id_B /some/where/a.flac

    # NOTE(kamo): SoundScpReader doesn't support pipe-fashion
    # like Kaldi e.g. "cat a.wav |".
    # NOTE(kamo): The audio signal is normalized to [-1,1] range.
    loader = SoundScpReader(path, normalize=True, always_2d=False)

    # SoundScpReader.__getitem__() returns Tuple[int, ndarray],
    # but ndarray is desired, so Adapter class is inserted here
    return AdapterForSoundScpReader(loader, float_dtype)


def midi_loader(path, float_dtype=None, rate=np.int32(24000)):
    # The file is as follows:
    #   utterance_id_A /some/where/a.mid
    #   utterance_id_B /some/where/b.midi

    loader = MIDIScpReader(fname=path, rate=rate)

    # MIDIScpReader.__getitem__() returns ndarray
    return AdapterForMIDIScpReader(loader)


def label_loader(path, float_dtype=None):
    # The file is as follows:
    #   utterance_id_A /some/where/a.mid
    #   utterance_id_B /some/where/b.midi

    loader = read_label(path)

    # MIDIScpReader.__getitem__() returns ndarray
    return AdapterForLabelScpReader(loader)


def kaldi_loader(path, float_dtype=None, max_cache_fd: int = 0):
    loader = kaldiio.load_scp(path, max_cache_fd=max_cache_fd)
    return AdapterForSoundScpReader(loader, float_dtype)


def rand_int_loader(filepath, loader_type):
    # e.g. rand_int_3_10
    try:
        low, high = map(int, loader_type[len("rand_int_") :].split("_"))
    except ValueError:
        raise RuntimeError(f"e.g rand_int_3_10: but got {loader_type}")
    return IntRandomGenerateDataset(filepath, low, high)


DATA_TYPES = {
    "sound": dict(
        func=sound_loader,
        kwargs=["float_dtype"],
        help="Audio format types which supported by sndfile wav, flac, etc."
        "\n\n"
        "   utterance_id_a a.wav\n"
        "   utterance_id_b b.wav\n"
        "   ...",
    ),
    # TODO(TaoQian)
    "midi": dict(
        func=midi_loader,
        kwargs=["float_dtype"],
        help="MIDI format types which supported by sndfile mid, midi, etc."
        "\n\n"
        "   utterance_id_a a.mid\n"
        "   utterance_id_b b.mid\n"
        "   ...",
    ),
    "duration": dict(
        func=label_loader,
        kwargs=[],
        help="Return text as is. The text must be converted to ndarray "
        "by 'preprocess'."
        "\n\n"
        "   utterance_id_A start_time_1 end_time_1 phone_1 start_time_2 end_time_2 phone_2 ...\n"
        "   utterance_id_B start_time_1 end_time_1 phone_1 start_time_2 end_time_2 phone_2 ...\n"
        "   ...",
    ),
    "kaldi_ark": dict(
        func=kaldi_loader,
        kwargs=["max_cache_fd"],
        help="Kaldi-ark file type."
        "\n\n"
        "   utterance_id_A /some/where/a.ark:123\n"
        "   utterance_id_B /some/where/a.ark:456\n"
        "   ...",
    ),
    "npy": dict(
        func=NpyScpReader,
        kwargs=[],
        help="Npy file format."
        "\n\n"
        "   utterance_id_A /some/where/a.npy\n"
        "   utterance_id_B /some/where/b.npy\n"
        "   ...",
    ),
    "text_int": dict(
        func=functools.partial(load_num_sequence_text, loader_type="text_int"),
        kwargs=[],
        help="A text file in which is written a sequence of interger numbers "
        "separated by space."
        "\n\n"
        "   utterance_id_A 12 0 1 3\n"
        "   utterance_id_B 3 3 1\n"
        "   ...",
    ),
    "csv_int": dict(
        func=functools.partial(load_num_sequence_text, loader_type="csv_int"),
        kwargs=[],
        help="A text file in which is written a sequence of interger numbers "
        "separated by comma."
        "\n\n"
        "   utterance_id_A 100,80\n"
        "   utterance_id_B 143,80\n"
        "   ...",
    ),
    "text_float": dict(
        func=functools.partial(load_num_sequence_text, loader_type="text_float"),
        kwargs=[],
        help="A text file in which is written a sequence of float numbers "
        "separated by space."
        "\n\n"
        "   utterance_id_A 12. 3.1 3.4 4.4\n"
        "   utterance_id_B 3. 3.12 1.1\n"
        "   ...",
    ),
    "csv_float": dict(
        func=functools.partial(load_num_sequence_text, loader_type="csv_float"),
        kwargs=[],
        help="A text file in which is written a sequence of float numbers "
        "separated by comma."
        "\n\n"
        "   utterance_id_A 12.,3.1,3.4,4.4\n"
        "   utterance_id_B 3.,3.12,1.1\n"
        "   ...",
    ),
    "text": dict(
        func=read_2column_text,
        kwargs=[],
        help="Return text as is. The text must be converted to ndarray "
        "by 'preprocess'."
        "\n\n"
        "   utterance_id_A hello world\n"
        "   utterance_id_B foo bar\n"
        "   ...",
    ),
    "hdf5": dict(
        func=H5FileWrapper,
        kwargs=[],
        help="A HDF5 file which contains arrays at the first level or the second level."
        "   >>> f = h5py.File('file.h5')\n"
        "   >>> array1 = f['utterance_id_A']\n"
        "   >>> array2 = f['utterance_id_B']\n",
    ),
    "rand_float": dict(
        func=FloatRandomGenerateDataset,
        kwargs=[],
        help="Generate random float-ndarray which has the given shapes "
        "in the file."
        "\n\n"
        "   utterance_id_A 3,4\n"
        "   utterance_id_B 10,4\n"
        "   ...",
    ),
    "rand_int_\\d+_\\d+": dict(
        func=rand_int_loader,
        kwargs=["loader_type"],
        help="e.g. 'rand_int_0_10'. Generate random int-ndarray which has the given "
        "shapes in the path. "
        "Give the lower and upper value by the file type. e.g. "
        "rand_int_0_10 -> Generate integers from 0 to 10."
        "\n\n"
        "   utterance_id_A 3,4\n"
        "   utterance_id_B 10,4\n"
        "   ...",
    ),
}


class AbsDataset(Dataset, ABC):
    @abstractmethod
    def has_name(self, name) -> bool:
        raise NotImplementedError

    @abstractmethod
    def names(self) -> Tuple[str, ...]:
        raise NotImplementedError

    @abstractmethod
    def __getitem__(self, uid) -> Tuple[Any, Dict[str, np.ndarray]]:
        raise NotImplementedError


class MuskitDataset(AbsDataset):
    """Pytorch Dataset class for Muskit.

    Examples:
        >>> dataset = MuskitDataset([('wav.scp', 'input', 'sound'),
        ...                          ('token_int', 'output', 'text_int')],
        ...                         )
        ... uttid, data = dataset['uttid']
        {'input': per_utt_array, 'output': per_utt_array}
    """

    def __init__(
        self,
        path_name_type_list: Collection[Tuple[str, str, str]],
        preprocess: Callable[
            [str, Dict[str, np.ndarray], float], Dict[str, np.ndarray]
        ] = None,
        float_dtype: str = "float32",
        int_dtype: str = "long",
        max_cache_size: Union[float, int, str] = 0.0,
        max_cache_fd: int = 0,
        not_align: list = ["text"],  # TODO(Tao): add to args
        mode: str = "valid",  # train, valid, plot_att, ...
        pitch_aug_min: int = 0,
        pitch_aug_max: int = 0,
        pitch_mean: str = "None",
        time_aug_min: float = 1.0,
        time_aug_max: float = 1.0,
        random_crop: bool = False,
        mask_aug: bool = False,
    ):
        assert check_argument_types()
        if len(path_name_type_list) == 0:
            raise ValueError(
                '1 or more elements are required for "path_name_type_list"'
            )

        path_name_type_list = copy.deepcopy(path_name_type_list)
        self.preprocess = preprocess

        self.float_dtype = float_dtype
        self.int_dtype = int_dtype
        self.max_cache_fd = max_cache_fd

        self.loader_dict = {}
        self.debug_info = {}
        for path, name, _type in path_name_type_list:
            if name in self.loader_dict:
                raise RuntimeError(f'"{name}" is duplicated for data-key')

            loader = self._build_loader(path, _type)
            self.loader_dict[name] = loader
            self.debug_info[name] = path, _type
            if len(self.loader_dict[name]) == 0:
                raise RuntimeError(f"{path} has no samples")

            # TODO(kamo): Should check consistency of each utt-keys?

        if isinstance(max_cache_size, str):
            max_cache_size = humanfriendly.parse_size(max_cache_size)
        self.max_cache_size = max_cache_size
        if max_cache_size > 0:
            self.cache = SizedDict(shared=True)
        else:
            self.cache = None
        self.not_align = not_align
        self.mode = mode

        self.pitch_aug_min = pitch_aug_min
        self.pitch_aug_max = pitch_aug_max
        self.pitch_mean = pitch_mean
        self.time_aug_min = time_aug_min
        self.time_aug_max = time_aug_max
        self.random_crop = random_crop
        self.mask_aug = mask_aug

        assert self.pitch_aug_min <= self.pitch_aug_max
        assert self.time_aug_min <= self.time_aug_max
        if self.pitch_mean != "None":
            assert self.pitch_aug_min == 0
            assert self.pitch_aug_max == 0

    def _build_loader(
        self, path: str, loader_type: str
    ) -> Mapping[str, Union[np.ndarray, torch.Tensor, str, numbers.Number]]:
        """Helper function to instantiate Loader.

        Args:
            path:  The file path
            loader_type:  loader_type. sound, npy, text_int, text_float, etc
        """
        for key, dic in DATA_TYPES.items():
            # e.g. loader_type="sound"
            # -> return DATA_TYPES["sound"]["func"](path)
            if re.match(key, loader_type):
                kwargs = {}
                for key2 in dic["kwargs"]:
                    if key2 == "loader_type":
                        kwargs["loader_type"] = loader_type
                    elif key2 == "float_dtype":
                        kwargs["float_dtype"] = self.float_dtype
                    elif key2 == "int_dtype":
                        kwargs["int_dtype"] = self.int_dtype
                    elif key2 == "max_cache_fd":
                        kwargs["max_cache_fd"] = self.max_cache_fd
                    else:
                        raise RuntimeError(f"Not implemented keyword argument: {key2}")

                func = dic["func"]
                try:
                    # logging.info(f"path: {path}")
                    return func(path, **kwargs)
                except Exception:
                    if hasattr(func, "__name__"):
                        name = func.__name__
                    else:
                        name = str(func)
                    logging.error(f"An error happend with {name}({path})")
                    raise
        else:
            raise RuntimeError(f"Not supported: loader_type={loader_type}")

    def has_name(self, name) -> bool:
        return name in self.loader_dict

    def names(self) -> Tuple[str, ...]:
        return tuple(self.loader_dict)

    def __iter__(self):
        return iter(next(iter(self.loader_dict.values())))

    def __repr__(self):
        _mes = self.__class__.__name__
        _mes += "("
        for name, (path, _type) in self.debug_info.items():
            _mes += f'\n  {name}: {{"path": "{path}", "type": "{_type}"}}'
        _mes += f"\n  preprocess: {self.preprocess})"
        return _mes

    def __getitem__(self, uid: Union[str, int]) -> Tuple[str, Dict[str, np.ndarray]]:
        assert check_argument_types()

        # Change integer-id to string-id
        if isinstance(uid, int):
            d = next(iter(self.loader_dict.values()))
            uid = list(d)[uid]

        if self.cache is not None and uid in self.cache:
            data = self.cache[uid]
            return uid, data

        if self.mode == "train":
            if self.pitch_mean != "None":
                loader = self.loader_dict["midi"]
                note_seq, tempo_seq = loader[(uid, 0, 1)]   # pitch_aug_factor = 0, global_time_aug_factor = 1

                sample_pitch_mean = np.mean(note_seq)

                if isinstance( eval(self.pitch_mean), float ):
                    # single dataset w/o spk-id
                    global_pitch_mean = float(self.pitch_mean)
                elif isinstance( eval(self.pitch_mean), list ):
                    # multi datasets with spk-ids
                    speaker_lst = ["oniku", "ofuton", "kiritan", "natsume"]     # NOTE: Fix me into args
                    _find_num = 0
                    _find_index = 0
                    for index in range(len(speaker_lst)):
                        if speaker_lst[index] in uid:
                            _find_num += 1
                            _find_index = index
                    assert _find_num == 1
                    global_pitch_mean = eval(self.pitch_mean)[_find_index]
                else:
                    ValueError("Not Support Type for pitch_mean: %s" % self.pitch_mean)

                gap = int((global_pitch_mean - sample_pitch_mean))
                if gap == 0:
                    lst = [0]
                elif gap < 0:
                    lst = [i for i in range(gap, 1)]
                else:
                    lst = [i for i in range(0, gap+1)]
                # logging.info(f"type: {type(note_seq)}, mean: {np.mean(note_seq)}, lst: {lst}, gap: {gap}")
                pitch_aug_factor = random.sample(lst, 1)[0]
            else:
                pitch_aug_factor = random.randint(self.pitch_aug_min, self.pitch_aug_max)

            _time_list = [
                i / 100
                for i in range(
                    int(self.time_aug_min * 100), int(self.time_aug_max * 100 + 1), 1
                )
            ]
            # _time_list = [1, 1.06, 1.12, 1.18, 1.24]
            # for _ in range(8):
            #     _time_list.append(1.0)
            time_aug_factor = random.sample(_time_list, 1)[0]
        else:
            pitch_aug_factor = 0
            time_aug_factor = 1

        data = {}
        # 1. Load data from each loaders
        for name, loader in self.loader_dict.items():
            # name: text, singing, label, midi
            try:
                if name == "midi" or name == "singing":
                    global_time_aug_factor = 1
                    value = loader[(uid, pitch_aug_factor, global_time_aug_factor)]
                else:
                    value = loader[uid]
                if isinstance(value, list):
                    value = np.array(value)
                if not isinstance(
                    value, (np.ndarray, torch.Tensor, str, numbers.Number, tuple)
                ):
                    raise TypeError(
                        f"Must be ndarray, torch.Tensor, str or Number: {type(value)}"
                    )
            except Exception:
                path, _type = self.debug_info[name]
                logging.error(
                    f"Error happened with path={path}, type={_type}, id={uid}"
                )
                raise

            # torch.Tensor is converted to ndarray
            if isinstance(value, torch.Tensor):
                value = value.numpy()
            elif isinstance(value, numbers.Number):
                value = np.array([value])
            data[name] = value

        # 2. [Option] Apply preprocessing
        #   e.g. muskit.train.preprocessor:CommonPreprocessor
        if self.preprocess is not None:
            data = self.preprocess(uid, data, time_aug_factor)

        length = min(
            [len(data[key]) for key in data.keys() if key not in self.not_align]
        )
        # logging.info(f"length: {length}")

        if self.mode == "train" and self.mask_aug:
            # mask_length = 4500      # 1500 = 300 * 5
            mask_length = random.randint(0, int(length * 0.2))
            if length - mask_length > 0:
                mask_index_begin = random.randint(0, int(length - mask_length))
                mask_index_end = mask_index_begin + mask_length
            else:
                mask_index_begin = 0
                mask_index_end = length

        if self.mode == "train" and self.random_crop:
            crop_length = random.randint(int(length * 0.8), length)
            crop_index_begin = random.randint(0, int(length - crop_length))
            crop_index_end = crop_index_begin + crop_length

        for key, value in data.items():
            if key in self.not_align:
                continue
            # logging.info(f"key: {key}, data[key].shape: {data[key].shape}")
            data[key] = data[key][:length]
            if self.mode == "train" and self.mask_aug:
                data[key][mask_index_begin:mask_index_end] = 0
            if self.mode == "train" and self.random_crop:
                data[key] = data[key][crop_index_begin:crop_index_end]

        # phone-level time augmentation

        # quit()
        data["pitch_aug"] = np.array([pitch_aug_factor])
        data["time_aug"] = np.array([time_aug_factor])

        # 3. Force data-precision
        for name in data:
            value = data[name]
            if not isinstance(value, (np.ndarray, tuple)):
                raise RuntimeError(
                    f"All values must be converted to np.ndarray object "
                    f'by preprocessing, but "{name}" is still {type(value)}.'
                )
            if isinstance(value, np.ndarray):
                # Cast to desired type
                if value.dtype.kind == "f":
                    value = value.astype(self.float_dtype)
                elif value.dtype.kind == "i":
                    value = value.astype(self.int_dtype)
                else:
                    raise NotImplementedError(f"Not supported dtype: {value.dtype}")
            data[name] = value

        if self.cache is not None and self.cache.size < self.max_cache_size:
            self.cache[uid] = data

        retval = uid, data
        # TODO allow the tuple type
        # assert check_return_type(retval)
        return retval
