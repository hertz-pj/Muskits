#!/usr/bin/env python3

"""Muskit setup script."""

import os

from distutils.version import LooseVersion
from setuptools import find_packages
from setuptools import setup


requirements = {
    "install": [
        "setuptools>=38.5.1",
        "configargparse>=1.2.1",
        "typeguard>=2.7.0",
        "dataclasses; python_version < '3.7'",
        "humanfriendly",
        "scipy>=1.4.1",
        "matplotlib==3.1.0",
        "pillow>=6.1.0",
        "editdistance==0.5.2",
        "ctc-segmentation<1.6,>=1.4.0",
        "wandb",
        "filelock",
        "pytsmod",
        "kaldiio",
        "kaldi-io",
        # DNN related packages are installed by Makefile
        # "chainer==6.0.0",
        # 'cupy==6.0.0',
        "tensorboard>=1.14",  # For pytorch>=1.1.0
        "tensorboardX>=1.8",  # For pytorch<1.1.0
        # Signal processing related
        "librosa>=0.8.0",
        # Natural language processing related
        # FIXME(kamo): Sentencepiece 0.1.90 breaks backwardcompatibility?
        "sentencepiece",
        "nltk>=3.4.5",
        # File IO related
        "PyYAML>=5.1.2",
        "soundfile>=0.10.2",
        "h5py>=2.10.0",
        "miditoolkit",
        "kaldiio",
        # SVS related
        "pyworld>=0.2.10",
        "torch_complex",
        "g2p_en",
    ],
    "recipe": [
        "gdown",
        "resampy",
        "pysptk>=0.1.17",
        "nnmnkwii",
        "museval>=0.2.1",
        "pystoi>=0.2.2",
        "mir-eval>=0.6",
        "fastdtw",
    ],
    "setup": ["numpy", "pytest-runner"],
    "test": [
        "pytest>=3.3.0",
        "pytest-timeouts>=1.2.1",
        "pytest-pythonpath>=0.7.3",
        "pytest-cov>=2.7.1",
        "hacking>=2.0.0",
        "mock>=2.0.0",
        "pycodestyle",
        "jsondiff>=1.2.0",
        "flake8>=3.7.8",
        "flake8-docstrings>=1.3.1",
        "black",
    ],
    "doc": [
        "Sphinx==2.1.2",
        "sphinx-rtd-theme>=0.2.4",
        "sphinx-argparse>=0.2.5",
        "commonmark==0.8.1",
        "recommonmark>=0.4.0",
        "travis-sphinx>=2.0.1",
        "nbsphinx>=0.4.2",
        "sphinx-markdown-tables>=0.0.12",
    ],
}
try:
    # NOTE: These packages are not listed if installing from the PyPI server
    import torch

    if LooseVersion(torch.__version__) >= LooseVersion("1.1.0"):
        requirements["install"].append("torch_optimizer")
    if LooseVersion(torch.__version__) >= LooseVersion("1.5.1"):
        requirements["install"].append("fairscale")

    elif LooseVersion(torch.__version__) >= LooseVersion("1.10.0"):
        requirements["install"].append("torchaudio==0.10.0")
    elif LooseVersion(torch.__version__) >= LooseVersion("1.9.1"):
        requirements["install"].append("torchaudio==0.9.1")
    elif LooseVersion(torch.__version__) >= LooseVersion("1.9.0"):
        requirements["install"].append("torchaudio==0.9.0")
    elif LooseVersion(torch.__version__) >= LooseVersion("1.8.2"):
        requirements["install"].append("torchaudio==0.8.2")
    elif LooseVersion(torch.__version__) >= LooseVersion("1.8.1"):
        requirements["install"].append("torchaudio==0.8.1")
    elif LooseVersion(torch.__version__) >= LooseVersion("1.8.0"):
        requirements["install"].append("torchaudio==0.8.0")
    elif LooseVersion(torch.__version__) >= LooseVersion("1.7.1"):
        requirements["install"].append("torchaudio==0.7.2")
    elif LooseVersion(torch.__version__) >= LooseVersion("1.7.0"):
        requirements["install"].append("torchaudio==0.7.0")
    elif LooseVersion(torch.__version__) >= LooseVersion("1.6.0"):
        # Due to https://github.com/pytorch/pytorch/issues/42213,
        # use torchaudio.functional.istft instead of torch.functional.istft
        requirements["install"].append("torchaudio==0.6.0")
    elif LooseVersion(torch.__version__) >= LooseVersion("1.5.1"):
        requirements["install"].append("torchaudio==0.5.1")
    elif LooseVersion(torch.__version__) >= LooseVersion("1.5.0"):
        requirements["install"].append("torchaudio==0.5.0")
    elif LooseVersion(torch.__version__) >= LooseVersion("1.4.0"):
        requirements["install"].append("torchaudio==0.4.0")
    elif LooseVersion(torch.__version__) >= LooseVersion("1.3.1"):
        requirements["install"].append("torchaudio==0.3.2")
    elif LooseVersion(torch.__version__) >= LooseVersion("1.3.0"):
        requirements["install"].append("torchaudio==0.3.1")
    elif LooseVersion(torch.__version__) >= LooseVersion("1.2.0"):
        requirements["install"].append("torchaudio==0.3.0")

    del torch
except ImportError:
    pass

install_requires = requirements["install"]
setup_requires = requirements["setup"]
tests_require = requirements["test"]
extras_require = {
    k: v for k, v in requirements.items() if k not in ["install", "setup"]
}

dirname = os.path.dirname(__file__)
version_file = os.path.join(dirname, "muskit", "version.txt")
with open(version_file, "r") as f:
    version = f.read().strip()
setup(
    name="muskit",
    version="0.0.1",
    url="https://github.com/SJTMusicTeam/Muskit",
    author="Shinji Watanabe",
    author_email="shinjiw@ieee.org",
    description="Muskit: music processing toolkit",
    long_description=open(os.path.join(dirname, "README.md"), encoding="utf-8").read(),
    long_description_content_type="text/markdown",
    license="Apache Software License",
    packages=find_packages(include=["music*"]),
    package_data={"music": ["version.txt"]},
    install_requires=install_requires,
    setup_requires=setup_requires,
    tests_require=tests_require,
    extras_require=extras_require,
    python_requires=">=3.6.0",
    classifiers=[
        "Programming Language :: Python",
        "Programming Language :: Python :: 3",
        "Programming Language :: Python :: 3.6",
        "Programming Language :: Python :: 3.7",
        "Programming Language :: Python :: 3.8",
        "Development Status :: 5 - Production/Stable",
        "Intended Audience :: Science/Research",
        "Operating System :: POSIX :: Linux",
        "License :: OSI Approved :: Apache Software License",
        "Topic :: Software Development :: Libraries :: Python Modules",
    ],
)
