"""Credential-scoped catalog identity; never store/print plaintext keys."""
from model.AIProviderKeys import provider_key_identity


def openai_identity(config):
    return provider_key_identity(config,'openai')
