"""Optional, fixed-revision CPU E5 encoding for a reviewed regional index."""
from __future__ import annotations

import json
import threading
from pathlib import Path


class E5:
    def __init__(self, model_dir: Path, identity: dict):
        model_dir = Path(model_dir).resolve()
        receipt = json.loads((model_dir / 'receipt.json').read_bytes())
        revision = receipt.get('revision', receipt.get('model_revision'))
        if revision != identity['revision'] or receipt.get('repo') != identity['model']:
            raise ValueError('Embedding weights do not match the index revision')
        files = receipt.get('files', [])
        declared = [item['file'] for item in files]
        if (len(declared) != len(set(declared)) or
                not {'model_qint8_avx512_vnni.onnx', 'tokenizer.json'}.issubset(declared)):
            raise ValueError('Model receipt must hash both the ONNX weights and tokenizer')
        import hashlib
        for item in files:
            path = model_dir / item['file']
            if not path.resolve().is_relative_to(model_dir.resolve()):
                raise ValueError('Model receipt path leaves its directory')
            with path.open('rb') as stream:
                if hashlib.file_digest(stream, 'sha256').hexdigest() != item['sha256']:
                    raise ValueError('Model weight/tokenizer hash differs from its receipt')
        import numpy as np
        import onnxruntime as ort
        from tokenizers import Tokenizer

        options = ort.SessionOptions()
        options.intra_op_num_threads = 2
        options.inter_op_num_threads = 1
        self.session = ort.InferenceSession(str(model_dir / 'model_qint8_avx512_vnni.onnx'), options,
                                            providers=['CPUExecutionProvider'])
        self.tokenizer = Tokenizer.from_file(str(model_dir / 'tokenizer.json'))
        self.tokenizer.enable_padding(pad_id=1, pad_token='<pad>')
        self.np = np
        self.dimensions = identity.get('dimensions', 384)
        self.lock = threading.Lock()

    def __call__(self, texts):
        return self.encode(texts)

    def encode(self, texts, *, query=False):
        np = self.np
        if not texts:
            return np.empty((0, self.dimensions), dtype=np.float32)
        prefix = 'query: ' if query else 'passage: '
        with self.lock:
            encoded = self.tokenizer.encode_batch([prefix + text for text in texts])
        if any(len(item.ids) > 512 for item in encoded):
            raise ValueError('Reviewed chunk exceeds the fixed E5 token budget')
        ids = np.asarray([item.ids for item in encoded], dtype=np.int64)
        mask = np.asarray([item.attention_mask for item in encoded], dtype=np.int64)
        hidden = self.session.run(None, {'input_ids': ids, 'attention_mask': mask,
                                        'token_type_ids': np.zeros_like(ids)})[0]
        pooled = (hidden * mask[:, :, None]).sum(1) / np.maximum(mask.sum(1)[:, None], 1)
        pooled /= np.maximum(np.linalg.norm(pooled, axis=1, keepdims=True), 1e-12)
        return pooled.astype(np.float32)
