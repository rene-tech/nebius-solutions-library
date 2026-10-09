from score_medical import alignment, words


def test_annotations_and_punctuation_do_not_become_reference_words():
    assert words("Hi! <UNSURE>don't know</UNSURE> <UNIN/>") == ["hi", "don't", "know"]


def test_wer_retains_substitutions_deletions_and_insertions():
    exact = alignment("I have diarrhea", "I have diarrhea.")
    assert exact["wer"] == 0 and exact["correct"] == 3
    assert alignment("no penicillin allergy", "penicillin allergy")["deletions"] == 1
    assert alignment("no penicillin allergy", "yes penicillin allergy")["substitutions"] == 1
    assert alignment("penicillin allergy", "no penicillin allergy")["insertions"] == 1
