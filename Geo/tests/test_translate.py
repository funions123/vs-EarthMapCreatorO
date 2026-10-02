"""Final byte resampling preserves categorical and continuous pixel values."""
import tempfile
import unittest
from pathlib import Path

import numpy as np
import rasterio
from PIL import Image

from pipeline.translate import _resize_byte_layer
from util.working import create_layer


class TranslateTests(unittest.TestCase):
    def test_global_resampling_matches_at_band_boundaries_and_downscaling(self):
        random = np.random.default_rng(20261001)
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            for shape, size in (((700, 500), (513, 1031)),
                                ((73, 61), (1051, 1043)),
                                ((1043, 61), (43, 519))):
                pixels = random.integers(0, 256, shape, dtype=np.uint8)
                source = directory / "source.tif"
                destination = directory / "resized.tif"
                with create_layer(source, shape[1], shape[0]) as output:
                    output.write(pixels, 1)
                for resampling in (Image.Resampling.NEAREST, Image.Resampling.BILINEAR):
                    with self.subTest(shape=shape, size=size, resampling=resampling):
                        _resize_byte_layer(source, destination, *size, resample=resampling)
                        with rasterio.open(destination) as output:
                            actual = output.read(1)
                        with Image.fromarray(pixels) as image:
                            expected = np.asarray(image.resize(size, resampling))
                        np.testing.assert_array_equal(actual, expected)
