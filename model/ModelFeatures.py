"""Keep embedding consumers compatible with Transformers 4 and 5."""


def feature_tensor(result):
    # Transformers 5 returns ModelOutput with the same projected feature tensor
    # in pooler_output; 4 returns that tensor directly. Do not change pooling or
    # normalization, which would invalidate existing search indexes.
    if hasattr(result, 'pooler_output'):
        features = result.pooler_output
        if features is None:
            raise RuntimeError('模型没有返回有效的投影特征')
        return features
    return result
