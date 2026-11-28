"""Attach ordered visual evidence to a transport copy after text projection and audit."""


def attach(payload, protocol, images):
    if not images:
        return payload
    supported = ('data:image/png;base64,', 'data:image/jpeg;base64,', 'data:image/webp;base64,')
    if any(not isinstance(image, str) or not image.startswith(supported) for image in images):
        raise ValueError('invalid_visual_attachment')
    if protocol == 'gemini-native':
        contents = list(payload['contents'])
        parts = []
        for image in images:
            header, data = image.split(',', 1)
            parts.append({'inlineData': {'mimeType': header[5:].split(';')[0], 'data': data}})
        if contents and contents[-1]['role'] == 'user':
            contents[-1] = {**contents[-1], 'parts': [*contents[-1]['parts'], *parts]}
        else:
            contents.append({'role': 'user', 'parts': parts})
        return {**payload, 'contents': contents}
    if protocol == 'openai-responses':
        key = 'input'
        parts = [{'type': 'input_image', 'image_url': image} for image in images]
    elif protocol in {'openai-chat', 'agy-local'}:
        key = 'messages'
        parts = [{'type': 'image_url', 'image_url': {'url': image}} for image in images]
    else:
        raise ValueError('structured_transport_requires_text')
    rows = list(payload[key])
    rows.append({'role': 'user', 'content': parts})
    return {**payload, key: rows}
