import unittest
from app_plugins.builtin.flow_assistant.prompt_segments import parse_segments,dialogue_text,speech_metrics

SAMPLE='''## 第1段｜8秒

**视频描述词：**\\
保持原图人物、服装及背景。第0秒立即讲话，第6秒轻轻点头。
**必须完整朗读，不得概括或省略。**

**台词：**\\
Tri proroctvá o posledných dňoch… v roku dvetisíc dvadsaťšesť ich uvidíme naplniť sa na vlastné oči.

---

## 第2段｜10秒

**视频描述词：**\\
延续同一次采访，不改人物身份。禁止字幕、UI、新增水印。

**台词：**\\
Keď budeš počuť tretie proroctvo, pochopíš, prečo je toto posolstvo také naliehavé. Proroctvo prvé: krvavý mesiac. Dňa tretieho

---

## 第3段｜10秒

**视频描述词：**\\
从侧前方中近景继续，第0秒承接上一段，不重复日期。

**台词：**\\
marca dvetisíc dvadsaťšesť spôsobí úplné zatmenie Slnka, že Mesiac sa sfarbí do krvavočervena. Nie je to teória,
'''


class SegmentTests(unittest.TestCase):
    def test_three_markdown_segments_preserve_every_body_and_spoken_ending(self):
        rows=parse_segments(SAMPLE);self.assertEqual(len(rows),3)
        self.assertEqual([r.detected_duration for r in rows],[8,10,10])
        self.assertIn('第6秒轻轻点头',rows[0].prompt)
        self.assertTrue(dialogue_text(rows[1].prompt).endswith('Dňa tretieho'))
        self.assertTrue(dialogue_text(rows[2].prompt).endswith('Nie je to teória,'))
        for part in ('Tri proroctvá','dvetisíc dvadsaťšesť','Proroctvo prvé:','marca'):
            self.assertIn(part,'\n'.join(r.prompt for r in rows))
        self.assertNotIn('## 第1段',rows[0].prompt)

    def test_variant_headers_and_description_pairs_without_headers(self):
        self.assertEqual(len(parse_segments('**第 一 段（６ 秒）**\n视频描述：保留。\n对白：A\n\n### Segment 2 | 10 seconds\n视频描述词：继续\nDialogue: B')),2)
        rows=parse_segments('视频描述词：第一幅\n台词：甲\n\n---\n视频描述：第二幅\n旁白：乙')
        self.assertEqual(len(rows),2);self.assertEqual(dialogue_text(rows[1].prompt),'乙')

    def test_ordinary_prose_is_not_split_and_preamble_is_not_silently_lost(self):
        text='保持第0秒立即讲话。\n\n镜头在第6秒缓慢推进。\n台词：hello: this stays intact.'
        self.assertEqual(len(parse_segments(text)),1);self.assertIn('hello: this stays intact.',parse_segments(text)[0].prompt)
        rows=parse_segments('请特别注意这段说明。\n## 第1段｜8秒\n台词：A')
        self.assertEqual(len(rows),2);self.assertIn('请特别注意',rows[0].prompt);self.assertTrue(rows[0].warning)

    def test_empty_heading_is_visible_not_dropped_and_empty_input_rejected(self):
        rows=parse_segments('第1段｜10秒\n第2段｜8秒\n台词：B')
        self.assertEqual(len(rows),2);self.assertFalse(rows[0].prompt);self.assertTrue(rows[0].warning)
        with self.assertRaises(ValueError):parse_segments('   ')

    def test_inline_description_and_numbered_duration_headers_are_not_lost(self):
        rows=parse_segments('1. 8秒\n台词：A\n2. 10秒\n台词：B')
        self.assertEqual(len(rows),2)
        rows=parse_segments('第1段｜8秒：保持人物不变。\n台词：A\n第2段｜10秒 视频描述词：保留这段描述\n台词：B')
        self.assertIn('保持人物不变。',rows[0].prompt);self.assertIn('保留这段描述',rows[1].prompt)

    def test_speech_counter_counts_required_rate_without_claiming_feasibility(self):
        speech,label=speech_metrics('视频描述：必须完整说出台词。\n**台词：**\\\nDňa tretieho marca.\n\n要求：无字幕',10)
        self.assertEqual(speech,'Dňa tretieho marca.')
        self.assertIn('3 词',label);self.assertIn('0.3 词/秒',label)
        self.assertNotIn('要求',speech)


if __name__=='__main__':unittest.main()
