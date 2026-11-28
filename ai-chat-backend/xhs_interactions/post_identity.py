"""One equivalence contract for matching and claiming an observed post."""
import unicodedata
from .store import digest


def author_name(value):
    return unicodedata.normalize('NFKC',str(value)).strip()


def identity_fields(note):
    return [note['profile_id'],note['note_title'],note['note_body'].strip()]


def source_ref(note):
    return digest(['xhs-post-source-v1',*identity_fields(note)])


def target_identity(note):
    return digest(['xhs-native-post-v1',*identity_fields(note)])


def matches_locator(note,locator):
    return bool(note and note['note_title']==locator['note_title']
                and note['note_body'].strip()==locator['note_body'].strip()
                and author_name(note['note_author'])==author_name(locator['note_author']))
