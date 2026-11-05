"""Multi-key settings with synthetic credentials, no real accounts/requests."""
import copy
import os
import unittest
from unittest.mock import patch

from qt_compat import QtWidgets
from model.AIProviderKeys import (KEYS_CONFIG_KEY,read_provider_key,credential_entry,key_pool,
    provider_key_identity,pin_provider_config,protect_key,cloudflare_account)
from tests.test_ai_model_client import UITests
from app_plugins.builtin.ai_model_client.settings import ClientSettingsPage
from app_plugins.builtin.ai_model_client.key_list import ProviderKeysEditor


class KeyPoolTests(unittest.TestCase):
    def pool(self):
        one=credential_entry('synthetic-one-1111','账号一')
        two=credential_entry('synthetic-two-2222','账号二')
        return {'version':1,'active':one['id'],'items':[one,two]}
    def test_legacy_key_remains_readable_and_is_wrapped_without_reencrypting(self):
        encrypted=protect_key('synthetic-legacy-key')
        pool=key_pool(encrypted)
        self.assertEqual(pool['items'][0]['encrypted'],encrypted)
        self.assertEqual(read_provider_key({KEYS_CONFIG_KEY:{'openai':encrypted}},'openai'),'synthetic-legacy-key')
    def test_only_selected_key_is_used_no_silent_rotation(self):
        pool=self.pool();config={KEYS_CONFIG_KEY:{'openai':pool}}
        self.assertEqual(read_provider_key(config,'openai'),'synthetic-one-1111')
        pool['active']=pool['items'][1]['id']
        self.assertEqual(read_provider_key(config,'openai'),'synthetic-two-2222')
        pool['active']=None
        with self.assertRaisesRegex(ValueError,'选择当前'):read_provider_key(config,'openai')
    def test_submission_snapshot_contains_only_active_cipher_and_is_immutable(self):
        pool=self.pool();config={KEYS_CONFIG_KEY:{'openai':pool,'mistral':protect_key('synthetic-other-key')}}
        pinned=pin_provider_config(config,'openai')
        self.assertEqual(pinned,{KEYS_CONFIG_KEY:{'openai':pool['items'][0]['encrypted']}})
        pool['active']=pool['items'][1]['id']
        self.assertEqual(read_provider_key(pinned,'openai'),'synthetic-one-1111')
    def test_catalog_identity_tracks_active_key_not_names_or_unused_keys(self):
        pool=self.pool();config={KEYS_CONFIG_KEY:{'openai':pool}}
        old=provider_key_identity(config,'openai')
        pool['items'][0]['name']='改备注';pool['items'].append(credential_entry('synthetic-three-3333','备用'))
        self.assertEqual(provider_key_identity(config,'openai'),old)
        pool['active']=pool['items'][1]['id']
        self.assertNotEqual(provider_key_identity(config,'openai'),old)
    def test_environment_can_be_explicitly_selected_with_saved_keys(self):
        pool=self.pool();pool['active']='@environment'
        with patch.dict(os.environ,{'OPENAI_API_KEY':'synthetic-env-key'}):
            self.assertEqual(read_provider_key({KEYS_CONFIG_KEY:{'openai':pool}},'openai'),'synthetic-env-key')
        with patch.dict(os.environ,{'OPENAI_API_KEY':''}),self.assertRaises(ValueError):
            read_provider_key({KEYS_CONFIG_KEY:{'openai':pool}},'openai')
    def test_cloudflare_account_follows_selected_key_and_survives_snapshot(self):
        one=credential_entry('synthetic-cf-one','一',account_id='a'*32)
        two=credential_entry('synthetic-cf-two','二',account_id='b'*32)
        pool={'version':1,'active':two['id'],'items':[one,two]}
        config={KEYS_CONFIG_KEY:{'cloudflare':pool},'ai_model_client':{'cloudflare_account_id':'c'*32}}
        self.assertEqual(cloudflare_account(config),'b'*32)
        self.assertEqual(cloudflare_account(pin_provider_config(config,'cloudflare')),'b'*32)
    def test_malformed_pool_is_not_sent_as_key_or_silently_fallback(self):
        for value in ({'version':1,'items':[],'active':'missing'}, {'version':3,'items':[]},
                      {'version':1,'active':None,'items':[{'id':'x','encrypted':'plaintext'}]}):
            with self.subTest(value=value),self.assertRaises(ValueError):
                read_provider_key({KEYS_CONFIG_KEY:{'openai':value}},'openai')


class KeyListUITests(unittest.TestCase):
    setUpClass=UITests.__dict__['setUpClass'];setUp=UITests.setUp;tearDown=UITests.tearDown;wait=UITests.wait
    def editor(self):
        editor=ProviderKeysEditor('openai');editor.load(None)
        self.addCleanup(editor.deleteLater)
        return editor
    def test_multiple_keys_visible_masked_and_counted(self):
        editor=self.editor()
        one=editor.add_key('synthetic-one-1111','付费账户')
        two=editor.add_key('synthetic-two-2222','赠送额度')
        self.assertEqual(editor.table.rowCount(),2);self.assertIn('已保存 2 个',editor.count.text())
        self.assertEqual(editor.pool['active'],one)
        visible=' '.join(editor.table.item(row,col).text() for row in range(2) for col in range(4))
        self.assertNotIn('synthetic-one',visible);self.assertIn('••••1111',visible)
        self.assertNotIn('synthetic-one-1111',str(editor.value()))
        editor.set_active(two)
        self.assertEqual(read_provider_key({KEYS_CONFIG_KEY:{'openai':editor.value()}},'openai'),'synthetic-two-2222')
    def test_duplicate_add_rejected_and_rename_keeps_cipher_and_identity(self):
        editor=self.editor();identifier=editor.add_key('synthetic-one-1111','旧名')
        before=copy.deepcopy(editor.value())
        with self.assertRaisesRegex(ValueError,'已经在列表'):editor.add_key('synthetic-one-1111','重复')
        editor.save_entry(identifier,'','新名字')
        self.assertEqual(editor.pool['items'][0]['encrypted'],before['items'][0]['encrypted'])
        self.assertEqual(editor.pool['items'][0]['id'],identifier)
    def test_delete_active_key_does_not_silently_switch_to_other_account(self):
        editor=self.editor();one=editor.add_key('synthetic-one-1111','一')
        two=editor.add_key('synthetic-two-2222','二')
        editor.remove_keys([one])
        self.assertEqual(editor.table.rowCount(),1);self.assertIn('未选择',editor.count.text())
        with self.assertRaises(ValueError):read_provider_key({KEYS_CONFIG_KEY:{'openai':editor.value()}},'openai')
        editor.set_active(two)
        self.assertEqual(read_provider_key({KEYS_CONFIG_KEY:{'openai':editor.value()}},'openai'),'synthetic-two-2222')
    def test_existing_legacy_key_is_displayed_and_untouched_settings_do_not_rewrite_it(self):
        encrypted=protect_key('synthetic-legacy-key')
        self.context.config[KEYS_CONFIG_KEY]={'openai':encrypted}
        page=ClientSettingsPage(self.context);self.addCleanup(page.widget.deleteLater)
        self.assertEqual(page.editors['openai'].table.rowCount(),1)
        config=self.context.load_config();page.update_config(config)
        self.assertEqual(config[KEYS_CONFIG_KEY]['openai'],encrypted)
        page.editors['openai'].add_key('synthetic-new-key','新Key');page.update_config(config)
        self.assertEqual(len(config[KEYS_CONFIG_KEY]['openai']['items']),2)
        self.assertEqual(config[KEYS_CONFIG_KEY]['openai']['items'][0]['encrypted'],encrypted)
    def test_external_key_edits_are_not_overwritten_by_stale_settings_page(self):
        page=ClientSettingsPage(self.context);self.addCleanup(page.widget.deleteLater)
        page.editors['openai'].add_key('synthetic-openai-new','我的新Key')
        config=self.context.load_config()
        encrypted=protect_key('synthetic-concurrent-change')
        config[KEYS_CONFIG_KEY]={'openai':encrypted}
        with self.assertRaisesRegex(ValueError,'其他窗口修改'):page.update_config(config)
        self.assertEqual(config[KEYS_CONFIG_KEY]['openai'],encrypted)
    def test_settings_total_and_provider_count_follow_add_remove(self):
        page=ClientSettingsPage(self.context);self.addCleanup(page.widget.deleteLater)
        one=page.editors['openai'].add_key('synthetic-one-1111','一')
        page.editors['groq'].add_key('synthetic-groq-2222','二')
        self.assertIn('共保存 2 个',page.total_keys.text())
        self.assertIn('1 个 Key',page.service.itemText(page.service.findData('openai')))
        page.editors['openai'].remove_keys([one])
        self.assertIn('共保存 1 个',page.total_keys.text())
    def test_apply_then_continue_editing_same_window_is_not_mistaken_for_external_change(self):
        page=ClientSettingsPage(self.context);self.addCleanup(page.widget.deleteLater)
        editor=page.editors['openai'];editor.add_key('synthetic-first-1111','一')
        config=self.context.load_config();page.update_config(config)
        editor.add_key('synthetic-second-2222','二');page.update_config(config)
        self.assertEqual(len(config[KEYS_CONFIG_KEY]['openai']['items']),2)
    def test_unselected_key_cannot_leave_a_pending_chat_message(self):
        editor=self.editor();one=editor.add_key('synthetic-one-1111','一')
        editor.add_key('synthetic-two-2222','二');editor.remove_keys([one])
        self.context.config[KEYS_CONFIG_KEY]={'openai':editor.value()}
        self.dialog.provider.setCurrentIndex(self.dialog.provider.findData('openai'))
        self.dialog.input.setPlainText('保留这句话')
        self.dialog.send()
        self.assertEqual(self.store.messages(self.dialog.current_id),[])
        self.assertEqual(self.dialog.input.toPlainText(),'保留这句话')
        self.assertIn('选择当前',self.dialog.status.text())
    def test_worker_and_catalog_reload_pin_new_active_selection(self):
        editor=self.editor();editor.add_key('synthetic-one-1111','一')
        two=editor.add_key('synthetic-two-2222','二')
        self.context.config[KEYS_CONFIG_KEY]={'openai':editor.value()}
        rows=[{'id':'gpt-6-luna','name':'test','free':None,'vision':None}]
        with patch.object(self.dialog.client,'list_models',return_value=rows) as listing:
            self.dialog.provider.setCurrentIndex(self.dialog.provider.findData('openai'))
            self.wait(lambda:self.dialog.handle is None and listing.call_count==1)
            editor.set_active(two);self.context.config[KEYS_CONFIG_KEY]['openai']=editor.value()
            self.dialog.settings_changed()
            self.wait(lambda:self.dialog.handle is None and listing.call_count==2)
            config=listing.call_args.args[1]
            self.assertIsInstance(config[KEYS_CONFIG_KEY]['openai'],str)
            self.assertEqual(read_provider_key(config,'openai'),'synthetic-two-2222')
