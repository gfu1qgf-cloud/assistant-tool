import copy
import random
from types import SimpleNamespace
import unittest
from model.node_workflow.text_palette import palette, contrast_ratio, measure, FAMILIES, CURATED, RANDOM_FAMILIES
from model.node_workflow.contracts import ColorValue
from app_plugins.builtin.batch_text_video.node_effects import fallback_registry


class TextPaletteTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from qt_compat import QtWidgets
        from app_plugins.builtin.batch_text_video.layout import ensure_fonts
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
        ensure_fonts()

    def setUp(self):
        definition=fallback_registry().definitions['core.text_palette']
        self.params={p.key:copy.deepcopy(p.default) for p in definition.parameters}
        self.context=SimpleNamespace(node_id='palette',task=SimpleNamespace(key='task1',directory='path'),
            workflow_globals=SimpleNamespace(iteration=1))

    def test_every_family_light_and_colored_meets_ratio_after_quantization_without_black(self):
        for family in [v[0] for v in FAMILIES]+['random','custom']:
            for style in ['light','colored']:
                for seed in range(40):
                    with self.subTest(family=family,style=style,seed=seed):
                        value=palette({},dict(self.params,family=family,style=style,seed=str(seed)),self.context)
                        for key in ['text','title']:
                            self.assertGreaterEqual(contrast_ratio(value[key],value['background']),7)
                            self.assertGreaterEqual(contrast_ratio(value[key],value['outline']),7)
                        self.assertEqual(value['background'].alpha,1)
                        self.assertNotEqual(value['background'].hex_rgb,'#000000')
                        rgb=value['background']
                        self.assertGreaterEqual(max(rgb.red,rgb.green,rgb.blue),.28)
                        self.assertGreater(max(rgb.red,rgb.green,rgb.blue)-min(rgb.red,rgb.green,rgb.blue),.08)

    def test_curated_zero_variation_uses_only_selected_swatches(self):
        self.assertEqual(len(CURATED), 14)
        self.assertEqual(set(CURATED), {key for key, _, _ in FAMILIES})
        for family, rows in CURATED.items():
            self.assertEqual(len(rows), 4)
            for style in ('light', 'colored'):
                for seed in range(40):
                    value = palette({}, dict(self.params, family=family, style=style,
                        variation=0, seed=str(seed)), self.context)
                    self.assertTrue(any(row[0] in value['description'] for row in rows))
                    field = 1 if style == 'light' else 2
                    self.assertIn(value['text'].hex_rgb, {row[field].lower() for row in rows})

    def test_video_defaults_and_no_brown_olive_foregrounds_even_in_existing_light_mode(self):
        import colorsys
        self.assertEqual(self.params['style'], 'colored')
        self.assertEqual(len(RANDOM_FAMILIES), 12)
        self.assertEqual(len(set(RANDOM_FAMILIES)), 12)
        self.assertFalse({'amber', 'terracotta'} & set(RANDOM_FAMILIES))
        for family in ('random', 'amber', 'terracotta'):
            for style in ('light', 'colored'):
                for seed in range(100):
                    value = palette({}, dict(self.params, family=family, style=style,
                        variation=30, seed=str(seed)), self.context)
                    self.assertGreater(max(value['text'].red, value['text'].green, value['text'].blue), .85
                        if style == 'colored' else 0)
                    for key in ('text', 'title', 'background', 'outline'):
                        color = value[key]
                        hue, saturation, brightness = colorsys.rgb_to_hsv(color.red, color.green, color.blue)
                        muddy = 10 <= hue*360 <= 100 and saturation > .15 and brightness < .80
                        self.assertFalse(muddy, (family, key, color.hex_rgb, hue*360))
                    if family == 'random':
                        self.assertTrue(any(row[0] in value['description'] for key in RANDOM_FAMILIES for row in CURATED[key]))

    def test_every_swatch_at_both_hue_shift_limits_stays_readable_and_not_muddy(self):
        import colorsys
        from model.node_workflow.text_palette import curated_colors
        class SwatchRng:
            def __init__(self, row, sign):
                self.row, self.sign = row, sign
            def choice(self, rows):
                return self.row
            def uniform(self, low, high):
                return {-1: low, 0: 0, 1: high}[self.sign]
        for family, rows in CURATED.items():
            for row in rows:
                for style in ('colored', 'light'):
                    for sign in (-1, 0, 1):
                        with self.subTest(family=family, swatch=row[0], style=style, sign=sign):
                            text, title, background, ratio, _ = curated_colors(
                                family, style, 30, 7, SwatchRng(row, sign))
                            self.assertGreaterEqual(ratio, 7)
                            for color in (text, title, background):
                                hue, saturation, brightness = colorsys.rgb_to_hsv(
                                    color.red, color.green, color.blue)
                                self.assertFalse(10 <= hue*360 <= 100 and saturation > .15 and brightness < .80,
                                    (family, row[0], color.hex_rgb))
                                self.assertGreaterEqual(max(color.red, color.green, color.blue), .28)
                            if style == 'colored' and family in RANDOM_FAMILIES:
                                self.assertGreaterEqual(max(text.red, text.green, text.blue), .90)
                                self.assertGreaterEqual(colorsys.rgb_to_hsv(text.red, text.green, text.blue)[1], .10)

    def test_random_distribution_is_broader_in_small_batches_not_a_global_shuffle(self):
        family_for_name = {row[0]: family for family in RANDOM_FAMILIES for row in CURATED[family]}
        counts = dict.fromkeys(RANDOM_FAMILIES, 0)
        batches, body_colors = [], set()
        for batch in range(200):
            families = set()
            for index in range(4):
                context = copy.deepcopy(self.context)
                context.task.key = f'batch-{batch}-task-{index}'
                value = palette({}, dict(self.params, variation=0), context)
                family = family_for_name[value['description'].split('；', 1)[0]]
                families.add(family)
                counts[family] += 1
                body_colors.add(value['text'].hex_rgb)
            batches.append(len(families))
        self.assertGreater(sum(batches)/len(batches), 3.4)
        self.assertTrue(all(35 <= count <= 100 for count in counts.values()), counts)
        self.assertGreaterEqual(len(body_colors), 44)

    def test_retries_subsets_and_reordered_tasks_keep_the_same_selection(self):
        def task_result(key):
            context = copy.deepcopy(self.context)
            context.task.key = key
            return palette({'seed': f'stable-{key}'}, self.params, context)
        expected = {key: task_result(key) for key in ('task-a', 'task-b', 'task-c', 'task-d')}
        for key in ('task-d', 'task-a', 'task-d', 'task-c'):
            self.assertEqual(task_result(key), expected[key])

    def test_legacy_card_choices_and_custom_colour_are_not_silently_changed_to_video_mode(self):
        import colorsys
        for family in [key for key, _, _ in FAMILIES] + ['custom']:
            value = palette({}, dict(self.params, family=family, style='light'), self.context)
            self.assertLess(colorsys.rgb_to_hls(value['text'].red, value['text'].green, value['text'].blue)[1], .5)
            self.assertGreater(colorsys.rgb_to_hls(value['background'].red, value['background'].green, value['background'].blue)[1], .8)

    def test_existing_port_keys_and_explicit_recommended_outline(self):
        definition = fallback_registry().definitions['core.text_palette']
        self.assertEqual([port.key for port in definition.outputs],
            ['text', 'title', 'background', 'contrast', 'description', 'outline'])
        from app_plugins.builtin.batch_text_video.workflow_colors import apply_color_inputs
        from qt_compat import QtGui
        for style in ('colored', 'light'):
            value = palette({}, dict(self.params, style=style), self.context)
            layer = apply_color_inputs({}, {'color': value['text'], 'outline_color': value['outline']})
            self.assertEqual(QtGui.QColor(layer['color']).name(), value['text'].hex_rgb)
            self.assertEqual(layer['outline_color'], value['outline'].hex_rgb)
            self.assertEqual(layer['outline_opacity'], 100)

    def test_real_text_renderer_keeps_selected_body_and_outline_rgb(self):
        from app_plugins.builtin.batch_text_video.components import new_text_component
        from app_plugins.builtin.batch_text_video.layers import normalize_layers
        from app_plugins.builtin.batch_text_video.text_components import render_text_box
        from app_plugins.builtin.batch_text_video.workflow_colors import apply_color_inputs
        for family, _, _ in FAMILIES:
            value = palette({}, dict(self.params, family=family), self.context)
            layer = new_text_component()
            layer.update(text='Božia láska', font='Open Sans', font_max=110, width=90,
                height=75, outline_width=5, background_enabled=False, shadow_enabled=False)
            layer = apply_color_inputs(layer, {'color': value['text'], 'outline_color': value['outline']})
            layer = normalize_layers([layer])[0]
            image, _ = render_text_box(layer, 640, 360)
            counts = dict.fromkeys([value['text'].hex_rgb, value['outline'].hex_rgb], 0)
            for y in range(0, image.height(), 2):
                for x in range(0, image.width(), 2):
                    color = image.pixelColor(x, y)
                    if color.alpha() == 255 and color.name() in counts:
                        counts[color.name()] += 1
            for key, count in counts.items():
                self.assertGreater(count, 20, (family, key, count))

    def test_known_contrast_and_no_rounding_at_threshold(self):
        white,black=ColorValue(1,1,1),ColorValue(0,0,0)
        self.assertAlmostEqual(contrast_ratio(white,black),21)
        self.assertEqual(contrast_ratio(white,white),1)
        gray=ColorValue.from_hex('#777777')
        self.assertFalse(measure({'text':gray,'background':white},{'minimum':4.5},None)['readable'])

    def test_repeatability_independent_tasks_and_global_rng(self):
        state=random.getstate(); a=palette({},self.params,self.context)
        self.assertEqual(a,palette({},self.params,self.context)); self.assertEqual(random.getstate(),state)
        self.context.task.key='task2'; self.assertNotEqual(a,palette({},self.params,self.context))
        fixed=dict(self.params,scope='fixed'); a=palette({},fixed,self.context)
        self.context.task.key='task3'; self.assertEqual(a,palette({},fixed,self.context))

    def test_optional_seed_iteration_and_custom_base(self):
        params=dict(self.params,family='custom')
        a=palette({'base':ColorValue.from_hex('#469368'),'seed':'1'},params,self.context)
        b=palette({'base':ColorValue.from_hex('#469368'),'seed':'1','iteration':2},params,self.context)
        self.assertNotEqual(a,b)
        self.assertGreaterEqual(a['contrast'],7)

    def test_bad_params_and_transparency_are_not_mislabeled_readable(self):
        for field,value in [('minimum',float('nan')),('minimum',3),('variation',31),('family','black'),('style','invalid')]:
            with self.subTest(field=field),self.assertRaises(ValueError):
                palette({},dict(self.params,**{field:value}),self.context)
        with self.assertRaisesRegex(ValueError,'透明'):
            contrast_ratio(ColorValue(1,1,1,.5),ColorValue(1,0,0))


if __name__=='__main__': unittest.main()
