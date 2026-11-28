"""Full-text embedding windows. Windows never become memory identities."""
from __future__ import annotations


class FullTextEmbedder:
    def __init__(self, embedder, query_instruction):
        self.embedder = embedder
        self.query_instruction = query_instruction

    def windows(self, text, *, prefix=''):
        self.embedder._ensure_model_loaded()
        tokenizer = self.embedder.model.tokenizer
        limit = int(self.embedder.model.max_seq_length)

        def fits(value):
            return len(tokenizer.encode(prefix + value, add_special_tokens=True,
                                         truncation=False)) <= limit

        if fits(text):
            return (text,)
        offsets = tokenizer(text, add_special_tokens=False, truncation=False,
                            return_offsets_mapping=True)['offset_mapping']
        budget = limit - len(tokenizer.encode(prefix, add_special_tokens=True)) - 4
        if budget < 1:
            raise ValueError('embedding_query_instruction_exceeds_window')
        cuts = [offsets[i][0] for i in range(budget, len(offsets), budget)] + [len(text)]
        pieces, start = [], 0
        for end in cuts:
            if end <= start:
                continue
            # Retokenizing a substring can change boundary tokens. Account for
            # the actual encoded substring rather than trusting character size.
            while not fits(text[start:end]):
                low, high = start + 1, end - 1
                while low < high:
                    mid = (low + high + 1) // 2
                    if fits(text[start:mid]):
                        low = mid
                    else:
                        high = mid - 1
                if not fits(text[start:low]):
                    raise ValueError('embedding_character_exceeds_window')
                pieces.append(text[start:low])
                start = low
            pieces.append(text[start:end])
            start = end
        return tuple(pieces)

    def embed_passages(self, texts, batch_size=32, *, query=False):
        import numpy as np
        prefix = self.query_instruction if query else ''
        windows = [self.windows(text, prefix=prefix) for text in texts]
        flat = [prefix + part for parts in windows for part in parts]
        vectors = self.embedder.embed_texts(flat, batch_size=batch_size)
        output, start = [], 0
        for parts in windows:
            output.append(np.asarray(vectors[start:start + len(parts)]))
            start += len(parts)
        return output

    def embed_query_passages(self, text):
        return self.embed_passages([text], query=True)[0]
