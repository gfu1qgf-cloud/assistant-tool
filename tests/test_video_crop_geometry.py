import unittest
from unittest.mock import patch
from app_plugins.builtin.batch_text_video.video_geometry import decoded_size
from app_plugins.builtin.batch_text_video.matting_video import describe
from app_plugins.builtin.batch_text_video.matting_live import StreamInput


class CroppedVideoGeometryTests(unittest.TestCase):
    def stream(self,**values):
        return dict(width=1088,height=1920,avg_frame_rate='30/1',
            side_data_list=[dict(side_data_type='Frame Cropping',crop_right=8)],**values)

    def test_cineform_mov_clean_aperture_and_preview_use_decoded_width(self):
        self.assertEqual(decoded_size(self.stream()),(1080,1920))
        info=describe(dict(video=self.stream(),duration=10))
        self.assertEqual((info['width'],info['height'],info['frames']),(1080,1920,300))

    def test_crop_precedes_autorotation(self):
        self.assertEqual(decoded_size(self.stream(tags=dict(rotate='90'))),(1920,1080))
        self.assertEqual(decoded_size(dict(width=1280,height=736,side_data_list=[
            dict(side_data_type='Frame Cropping',crop_bottom=16),dict(rotation=-90)])),(720,1280))

    def test_explicit_display_dimensions_do_not_apply_crop_twice(self):
        self.assertEqual(decoded_size(self.stream(display_width=1080,display_height=1920)),(1080,1920))
        self.assertEqual(decoded_size(dict(width=720,height=1280)),(720,1280))

    def test_invalid_crop_or_rotation_cannot_create_bad_raw_frame_sizes(self):
        for stream in (dict(width=8,height=10,side_data_list=[dict(side_data_type='Frame Cropping',crop_right=8)]),
                       dict(width=8,height=10,side_data_list=[dict(side_data_type='Frame Cropping',crop_left=-1)]),
                       dict(width=8,height=10,tags=dict(rotate=float('nan')))):
            with self.subTest(stream=stream),self.assertRaises(ValueError):decoded_size(stream)

    def test_deferred_raw_input_agrees_with_autocropped_decoder_and_remains_lazy(self):
        binding=dict(source=dict(path='transparent.mov'))
        with patch('app_plugins.builtin.batch_text_video.engine.resolve_tools',return_value=('ffmpeg','ffprobe')), \
                patch('app_plugins.builtin.batch_text_video.engine.probe',return_value=dict(video=self.stream(),duration=10)), \
                patch('subprocess.Popen') as process:
            spec=StreamInput(binding,20,True,30,'ffmpeg')
        self.assertEqual((spec.width,spec.height),(1080,1920))
        self.assertIn('1080x1920',spec.options())
        process.assert_not_called()
