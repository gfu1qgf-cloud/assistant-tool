"""Capability/transport/UI/citation contracts, without real accounts or searches."""
import copy
import unittest
from unittest.mock import patch
from model.AIChatCapabilities import capabilities,apply_options
from model.AIChatSources import response_metadata,text_with_sources
from model.OpenAIResponses import normalize_response
from model.AIProviderStream import ChatStream
from tests.test_ai_model_client import UITests,ProviderTests,Response
from tests.test_ai_client_stream import SSEResponse
from app_plugins.builtin.ai_model_client.rendering import cited_message_html


class CapabilityTests(unittest.TestCase):
    def test_openai_efforts_are_model_specific_and_sampling_depends_on_effort(self):
        self.assertNotIn('none',capabilities('openai','gpt-6-astra')['efforts'])
        self.assertNotIn('none',capabilities('openai','gpt-6.1-sol')['efforts'])
        self.assertIn('none',capabilities('openai','gpt-6-luna')['efforts'])
        self.assertFalse(capabilities('openai','gpt-6-luna')['sampling'])
        self.assertTrue(capabilities('openai','gpt-6-luna',options={'reasoning_effort':'none'})['sampling'])
        self.assertFalse(capabilities('openai','gpt-4.1')['efforts'])
        self.assertTrue(capabilities('openai','gpt-4.1')['sampling'])

    def test_gemini_versions_and_budget_restrictions(self):
        self.assertTrue(capabilities('gemini','gemini-2.5-pro')['budget'])
        self.assertFalse(capabilities('gemini','gemini-2.5-pro')['budget'][2])
        self.assertEqual(capabilities('gemini','gemini-3.1-pro-preview')['efforts'],('low','high'))
        self.assertNotIn('minimal',capabilities('gemini','gemini-3.8-flash')['efforts'])
        self.assertIn('minimal',capabilities('gemini','gemini-3.5-flash')['efforts'])
        self.assertFalse(capabilities('gemini','gemini-unknown')['efforts'])
        self.assertFalse(capabilities('gemini','gemini-2.5-flash-image')['budget'])
        self.assertFalse(capabilities('gemini','gemini-3.1-pro-preview')['web'])

    def test_router_capabilities_use_model_metadata_not_publisher_guesses(self):
        self.assertFalse(capabilities('openrouter','openai/anything')['efforts'])
        self.assertIn('high',capabilities('openrouter','a/b',{'supported_parameters':['reasoning']})['efforts'])
        self.assertFalse(capabilities('openrouter','a/b',options={'free_only':True})['web'])
        self.assertTrue(capabilities('openrouter','a/b',options={'free_only':False})['web'])

    def test_unsupported_options_are_rejected_before_transport(self):
        for provider,model,options in (
            ('openai','gpt-6-astra',{'reasoning_effort':'none'}),
            ('openai','gpt-6.1-sol',{'temperature':.5}),
            ('openai','gpt-4.1',{'reasoning_effort':'high'}),
            ('gemini','gemini-2.5-pro',{'thinking_budget':0}),
            ('mistral','mistral-large-4',{'reasoning_effort':'max'}),
            ('groq','openai/gpt-oss-20b',{'reasoning_effort':'none'}),
            ('cloudflare','@cf/meta/llama-test',{'web_search':True}),
        ):
            with self.subTest(model=model,options=options),self.assertRaises(ValueError):
                apply_options(provider,model,{'generationConfig':{}},options)

    def test_non_openai_reasoning_payloads_do_not_reuse_wrong_schema(self):
        payload={'generationConfig':{}}
        apply_options('gemini','gemini-3.1-pro-preview',payload,{'reasoning_effort':'high'})
        self.assertEqual(payload['generationConfig']['thinkingConfig'],{'thinkingLevel':'HIGH'})
        payload={'generationConfig':{}}
        apply_options('gemini','gemini-2.5-flash',payload,{'thinking_budget':4096,'top_p':.8})
        self.assertEqual(payload['generationConfig']['thinkingConfig'],{'thinkingBudget':4096})
        self.assertEqual(payload['generationConfig']['topP'],.8)
        for provider,model in (('groq','openai/gpt-oss-120b'),('mistral','mistral-large-4')):
            payload={};apply_options(provider,model,payload,{'reasoning_effort':'high'})
            self.assertEqual(payload,{'reasoning_effort':'high'})

    def test_web_only_when_enabled_and_never_implicitly_added(self):
        payload={};apply_options('openai','gpt-6-luna',payload,{})
        self.assertNotIn('tools',payload)
        apply_options('openai','gpt-6-luna',payload,{'web_search':True,'search_limit':2,
            'reasoning_effort':'high','reasoning_mode':'pro','verbosity':'high'})
        self.assertEqual(payload['tools'],[{'type':'web_search'}]);self.assertEqual(payload['max_tool_calls'],2)
        self.assertEqual(payload['reasoning'],{'effort':'high','mode':'pro'})
        self.assertEqual(payload['text'],{'verbosity':'high'})
        apply_options('openai','gpt-6-luna',payload,{'web_search':True,'search_mode':'required'})
        self.assertEqual(payload['tool_choice'],'required')


class CapabilityUITests(unittest.TestCase):
    setUpClass=UITests.__dict__['setUpClass'];setUp=UITests.setUp;tearDown=UITests.tearDown;wait=UITests.wait
    def test_controls_disable_without_erasing_saved_values_and_restore_on_switch(self):
        with patch.dict('os.environ',{'OPENAI_API_KEY':''}):
            self.dialog.provider.setCurrentIndex(self.dialog.provider.findData('openai'))
            self.dialog.model.setCurrentText('gpt-6.1-sol')
            controls=self.dialog.advanced
            controls.reasoning.setCurrentIndex(controls.reasoning.findData('high'))
            controls.web.setChecked(True)
            controls.verbosity.setCurrentIndex(controls.verbosity.findData('high'))
            self.dialog.temperature_enabled.setChecked(True)
            self.assertFalse(self.dialog.temperature_enabled.isEnabled())
            self.assertIsNone(self.dialog.request_options()['temperature'])
            self.dialog.model.setCurrentText('gpt-4.1')
            self.assertFalse(controls.reasoning.isEnabled())
            self.assertFalse(controls.verbosity.isEnabled())
            self.assertEqual(self.dialog.request_options()['reasoning_effort'],'default')
            self.assertEqual(self.dialog.options()['reasoning_effort'],'high')
            self.dialog.model.setCurrentText('gpt-6.1-sol')
            self.assertEqual(self.dialog.request_options()['reasoning_effort'],'high')
            self.dialog.save_draft()
            saved=self.store.get(self.dialog.current_id)['options']
            self.assertTrue(saved['web_search']);self.assertEqual(saved['verbosity'],'high')
            self.assertIn('OpenAI / GPT',self.dialog.history.currentItem().text())
            self.assertFalse(self.dialog.free_only.isEnabled())
            self.assertFalse(self.dialog.cf_free_only.isEnabled())

    def test_default_web_off_and_unknown_model_special_features_disabled(self):
        self.assertFalse(self.dialog.advanced.web.isChecked())
        self.dialog.provider.setCurrentIndex(self.dialog.provider.findData('cloudflare'))
        self.dialog.model.setCurrentText('@cf/meta/unknown')
        self.assertFalse(self.dialog.advanced.web.isEnabled())
        self.assertFalse(self.dialog.advanced.reasoning.isEnabled())
        self.assertFalse(self.dialog.request_options()['web_search'])


class CapabilityTransportTests(unittest.TestCase):
    setUp=ProviderTests.setUp;tearDown=ProviderTests.tearDown
    def test_openai_transport_receives_optional_tools_effort_and_verbosity(self):
        data={'status':'completed','model':'gpt-6-luna','output':[
            {'type':'web_search_call','action':{'type':'search'}},
            {'type':'message','content':[{'type':'output_text','text':'有依据的回答',
              'annotations':[{'type':'url_citation','url':'https://example.com/source','title':'来源','start_index':0,'end_index':6}]}]}],
            'usage':{'input_tokens':10,'output_tokens':20,'output_tokens_details':{'reasoning_tokens':12}}}
        messages=[{'role':'user','text':'搜索','images':[]}]
        for streaming in (False,True):
            response=SSEResponse([{'type':'response.completed','response':data}]) if streaming else Response(data)
            with patch('model.AIProviderClient.requests.request',return_value=response) as request:
                result=self.client.chat('openai','gpt-6-luna',messages,{'web_search':True,
                    'reasoning_effort':'high','verbosity':'low','stream':streaming},self.config,self.job)
            payload=request.call_args.kwargs['json']
            self.assertEqual(payload['reasoning']['effort'],'high');self.assertEqual(payload['text']['verbosity'],'low')
            self.assertEqual(result['search_calls'],1);self.assertEqual(result['reasoning_tokens'],12)
            self.assertEqual(result['sources'][0]['url'],'https://example.com/source')


class CitationTests(unittest.TestCase):
    def test_inline_citations_are_clickable_and_source_html_is_escaped(self):
        html=cited_message_html('事实。',{'sources':[{'url':'https://example.com/a?x=1&y=2','title':'<script>bad</script>','start':0,'end':3}]})
        self.assertIn('href="https://example.com/a?x=1&amp;y=2"',html)
        self.assertIn('[1]</a>',html);self.assertNotIn('<script>',html)
        self.assertIn('事实',html)
    def test_non_web_citation_links_are_never_rendered_or_loaded(self):
        html=cited_message_html('原文',{'sources':[{'url':'file:///C:/private.txt','title':'本地'},
              {'url':'javascript:alert(1)','title':'危险'},{'url':'https://user:pass@example.com','title':'带凭据'}]})
        self.assertNotIn('href=',html);self.assertNotIn('private',html)
    def test_gemini_grounding_and_reasoning_usage_metadata(self):
        result=response_metadata('gemini',{'candidates':[{'groundingMetadata':{
            'groundingChunks':[{'web':{'uri':'https://example.com','title':'来源'}}],
            'groundingSupports':[{'segment':{'endIndex':2},'groundingChunkIndices':[0]}],
            'webSearchQueries':['query']}}],'usageMetadata':{'thoughtsTokenCount':15}})
        self.assertEqual(result['reasoning_tokens'],15);self.assertEqual(result['search_queries'],1)
        self.assertTrue(result['sources']);self.assertIsNone(result['search_calls'])
    def test_copy_export_and_followup_context_can_keep_verified_source_urls(self):
        text=text_with_sources('答案',{'sources':[{'url':'https://example.com/s','title':'来源'},
               {'url':'https://example.com/s','title':'重复'},{'url':'file:///secret','title':'危险'}]})
        self.assertEqual(text.count('https://example.com/s'),1);self.assertNotIn('file:',text)

    def test_metadata_survives_completed_request_and_is_available_in_export_and_context(self):
        import tempfile
        from pathlib import Path
        from app_plugins.builtin.ai_model_client.store import ChatStore
        with tempfile.TemporaryDirectory() as folder:
            store=ChatStore(Path(folder)/'history'); identifier=store.create('openai','gpt-6-luna')
            prompt=store.begin_request(identifier,'问题',[],'openai','gpt-6-luna')
            store.complete_request(identifier,prompt,{'text':'答案','provider':'openai','model':'gpt-6-luna',
                'usage':{},'elapsed':1,'warning':'','sources':[{'url':'https://example.com/s','title':'来源'}],
                'web_enabled':True,'search_calls':1,'reasoning_tokens':123})
            next_prompt=store.begin_request(identifier,'追问',[],'openai','gpt-6-luna')
            self.assertIn('https://example.com/s',store.request_messages(identifier,next_prompt,20)[1]['text'])
            self.assertIn('https://example.com/s',store.export_text(identifier))
            self.assertEqual(store.messages(identifier)[1]['metadata']['search_calls'],1)
