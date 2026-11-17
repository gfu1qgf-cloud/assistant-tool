"""Exact geometry oracles must not depend on lossy encoding or host CPU count."""
from contextlib import contextmanager
from unittest.mock import patch


@contextmanager
def lossless_composition(*, filter_threads=1):
    def encoder_args(encoder, **_kwargs):
        if encoder != 'cpu':
            raise AssertionError('Exact-pixel fixture requires the CPU encoder')
        return ['-c:v','libx264','-preset','ultrafast','-crf','0','-threads','1']

    with patch('model.MediaEncoding.video_encoding_args',side_effect=encoder_args), \
            patch('model.MediaEncoding.filter_thread_args',return_value=[
                '-filter_threads',str(filter_threads),'-filter_complex_threads',str(filter_threads)]):
        yield
