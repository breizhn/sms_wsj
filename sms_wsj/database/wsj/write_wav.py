"""
Example calls:
python -m sms_wsj.database.wsj.write_wav with dst_dir=/DEST/DIR wsj_root=/WSJ/ROOT/DIR --sample_rate=8000

mpiexec -np 20 python -m sms_wsj.database.wsj.write_wav with dst_dir=/DEST/DIR wsj_root=/WSJ/ROOT/DIR sample_rate=8000

"""

import os
import fnmatch
import shutil
import subprocess
import tempfile
from pathlib import Path

import numpy as np
import sacred
import soundfile
import warnings

import dlp_mpi

ex = sacred.Experiment('Write WSJ waves')

kaldi_root = Path(os.environ['KALDI_ROOT'])


def read_nist_wsj(path, expected_sample_rate=16000):
    """
    Converts a nist/sphere file of wsj and reads it with soundfile.

    :param path: file path to audio file.
    :param audioread_function: Function to use to read the resulting audio file
    :return:
    """
    tmp_file = tempfile.NamedTemporaryFile(delete=False)
    cmd = "{}/sph2pipe -f wav '{path}' '{dest_file}'".format(
        kaldi_root / 'tools/sph2pipe_v2.5', path=path, dest_file=tmp_file.name
    )
    subprocess.run(
        cmd, universal_newlines=True, shell=True, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, check=True, env=None, cwd=None
    )
    with soundfile.SoundFile(tmp_file.name, mode='r') as f:
        assert f.samplerate == expected_sample_rate, f.samplerate
        signal = f.read()
    os.remove(tmp_file.name)
    return signal


def resample_with_sox(x, rate_in, rate_out):
    if rate_in == rate_out:
        return x

    x = x.astype(np.float32)
    command = (
        f'sox -N -V1 -t f32 -r {rate_in} -c 1 - -t f32 -r {rate_out} -c 1 -'
    ).split()
    process = subprocess.run(
        command,
        shell=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        input=x.tobytes(order="f")
    )
    return np.fromstring(process.stdout, dtype=np.float32)


@ex.config
def config():
    dst_dir = None
    wsj_root = None
    wsj0_root = wsj_root
    wsj1_root = wsj_root
    sample_rate = 16000
    assert dst_dir is not None, 'You have to specify a destination dir'
    assert wsj0_root is not None, 'You have to specify a wsj0_root'
    assert wsj1_root is not None, 'You have to specify a wsj1_root'


@ex.automain
def write_wavs(dst_dir: Path, wsj0_root: Path, wsj1_root: Path, sample_rate):
    wsj0_root = Path(wsj0_root).expanduser().resolve()
    wsj1_root = Path(wsj1_root).expanduser().resolve()
    dst_dir = Path(dst_dir).expanduser().resolve()
    assert wsj0_root.exists(), wsj0_root
    assert wsj1_root.exists(), wsj1_root

    assert not dst_dir == wsj0_root, (wsj0_root, dst_dir)
    assert not dst_dir == wsj1_root, (wsj1_root, dst_dir)
    # Expect, that the dst_dir does not exist to make sure to not overwrite.
    if dlp_mpi.IS_MASTER:
        dst_dir.mkdir(parents=True, exist_ok=False)

    if dlp_mpi.IS_MASTER:
        # Search for CD numbers, e.g. "13-34.1"
        # CD stands for compact disk.
        cds_0 = list(wsj0_root.rglob("*-*.*"))
        cds_1 = list(wsj1_root.rglob("*-*.*"))
        cds = set(cds_0 + cds_1)

        expected_number_of_files = {
            'pl': 3, 'ndx': 106, 'ptx': 3547, 'dot': 3585, 'txt': 256
        }
        number_of_written_files = dict()
        for suffix in expected_number_of_files.keys():
            files_0 = list(wsj0_root.rglob(f"*.{suffix}"))
            files_1 = list(wsj1_root.rglob(f"*.{suffix}"))
            files = set(files_0 + files_1)
            # Filter files that do not have a folder that matches "*-*.*".
            files = {
                file
                for file in files
                if any([fnmatch.fnmatch(part, "*-*.*") for part in file.parts])
            }

            # the readme.txt file in the parent directory is not copied
            print(f"About to write ca. {len(files)} {suffix} files.")
            for cd in cds:
                cd_files = list(cd.rglob(f"*.{suffix}"))
                for file in cd_files:
                    target = dst_dir / file.relative_to(cd.parent)
                    target.parent.mkdir(parents=True, exist_ok=True)
                    if not target.is_file():
                        shutil.copy(file, target.parent)
            number_of_written_files[suffix] = len(
                list(dst_dir.rglob(f"*.{suffix}"))
            )
            print(
                f"Writing {number_of_written_files[suffix]} {suffix} files."
            )
            print(
                f'Expected {expected_number_of_files[suffix]} {suffix} files.'
            )

        for suffix in expected_number_of_files.keys():
            message = (
                f'Expected that '
                f'{expected_number_of_files[suffix]} '
                f'files with the {suffix} are written. '
                f'But only {number_of_written_files} are written.'
            )
            if (
                number_of_written_files[suffix]
                != expected_number_of_files[suffix]
            ):
                if suffix in 'ndx ptx dot'.split():
                    raise RuntimeError(message)
                else:
                    warnings.warn(message)

    if dlp_mpi.IS_MASTER:
        # Ignore .wv2 files since they are not referenced in our database
        # anyway
        wsj_nist_files = [(cd, nist_file) for cd in cds
                          for nist_file in cd.rglob("*.wv1")]
        print(f"About to write {len(wsj_nist_files)} wav files.")
    else:
        wsj_nist_files = None

    wsj_nist_files = dlp_mpi.bcast(wsj_nist_files)

    for nist_file_tuple in dlp_mpi.split_managed(wsj_nist_files):
        cd, nist_file = nist_file_tuple
        assert isinstance(nist_file, Path), nist_file
        signal = read_nist_wsj(nist_file, expected_sample_rate=16000)
        file = nist_file.with_suffix('.wav')
        target = dst_dir / file.relative_to(cd.parent)
        assert not target == nist_file, (nist_file, target)
        target.parent.mkdir(parents=True, exist_ok=True)
        signal = resample_with_sox(signal, rate_in=16000, rate_out=sample_rate)
        # normalization to mean 0:
        signal -= np.mean(signal)
        # normalization:
        #   Correction, because the allowed values are in the range [-1, 1).
        #       => "1" is not a vaild value
        correction = (2 ** 15 - 1) / (2 ** 15)
        signal = signal * (correction / np.amax(np.abs(signal)))
        with soundfile.SoundFile(
                str(target), samplerate=sample_rate, channels=1,
                subtype='FLOAT', mode='w',
        ) as f:
            f.write(signal.T)

    dlp_mpi.barrier()
    if dlp_mpi.IS_MASTER:
        created_files = list(set(list(dst_dir.rglob("*.wav"))))
        print(f"Written {len(created_files)} wav files.")
        assert len(wsj_nist_files) == len(created_files), (len(wsj_nist_files), len(created_files))
