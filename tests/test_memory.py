import pytest

from app.memory import AnswerMemory
from src.models import Job


def job(board='example', location='Bengaluru, India', source_id='123'):
    return Job(source='greenhouse', source_id=source_id, board=board, company=board.title(),
               title='Engineer', description='Example job', location=location,
               url=f'https://job-boards.greenhouse.io/{board}/jobs/{source_id}',
               apply_url=f'https://job-boards.greenhouse.io/{board}/jobs/{source_id}')


def test_questions_are_remembered_per_applicant_and_reused_only_in_scope(tmp_path):
    memory = AnswerMemory(tmp_path)
    question = {'label': 'Will you require sponsorship? *', 'kind': 'choice', 'options': ['Yes', 'No']}
    memory.record_questions('one@example.com', job(), [question])
    stored = memory.questions('one@example.com')
    assert len(stored) == 1 and stored[0]['pending'] and stored[0]['answer'] == ''
    memory.save_answer('one@example.com', stored[0]['id'], 'No', reuse_globally=False)
    assert memory.lookup('one@example.com', job(), question) == 'No'
    assert memory.lookup('two@example.com', job(), question) is None
    assert memory.lookup('one@example.com', job(board='other'), question) is None
    assert memory.lookup('one@example.com', job(location='Mumbai, India'), question) is None
    assert memory.lookup('one@example.com', job(), question | {'label': 'Will you NOT require sponsorship?'}) is None
    memory.save_answer('one@example.com', stored[0]['id'], 'No', reuse_globally=True)
    assert memory.lookup('one@example.com', job(board='other'), question) == 'No'


def test_choice_answers_must_match_an_available_option_and_can_be_forgotten(tmp_path):
    memory = AnswerMemory(tmp_path)
    question = {'label': 'Current country of residence?', 'kind': 'choice', 'options': ['India', 'Canada']}
    memory.record_questions('candidate@example.com', job(), [question])
    question_id = memory.questions('candidate@example.com')[0]['id']
    with pytest.raises(ValueError):
        memory.save_answer('candidate@example.com', question_id, 'Made-up choice', reuse_globally=False)
    memory.save_answer('candidate@example.com', question_id, 'India', reuse_globally=False)
    assert memory.lookup('candidate@example.com', job(), question | {'options': ['Canada']}) is None
    assert memory.lookup('candidate@example.com', job(), question | {'options': ['India', 'INDIA']}) is None
    assert not memory.delete_question('other@example.com', question_id)
    assert memory.delete_question('candidate@example.com', question_id)
    assert memory.questions('candidate@example.com') == []


def test_choice_placeholder_is_never_saved_as_an_answer(tmp_path):
    memory = AnswerMemory(tmp_path)
    question = {'label': 'Country?', 'kind': 'choice', 'options': ['Select...', 'India']}
    memory.record_questions('candidate@example.com', job(), [question])
    stored = memory.questions('candidate@example.com')[0]
    assert stored['options'] == ['India']
    with pytest.raises(ValueError):
        memory.save_answer('candidate@example.com', stored['id'], 'Select...', reuse_globally=False)
    with pytest.raises(ValueError):
        memory.add_answer('candidate@example.com', 'Country?', 'choice', 'Select...')


def test_profile_is_explicitly_saved_and_never_accepts_api_keys(tmp_path):
    memory = AnswerMemory(tmp_path)
    profile = {'first_name': 'Avery', 'last_name': 'Example', 'phone': '+1 555 0100',
               'current_city': 'Example City', 'current_country': 'Example Country'}
    memory.save_profile('candidate@example.com', profile)
    assert memory.profile('candidate@example.com') == profile
    assert memory.profile('other@example.com') == {}
    with pytest.raises(ValueError):
        memory.save_profile('candidate@example.com', {'api_key': 'dummy-secret-key'})
    with pytest.raises(ValueError):
        memory.save_profile('candidate@example.com', {'phone': '1' * 41})
    with pytest.raises(ValueError):
        memory.save_profile('candidate@example.com', {'current_city': 'sk-test-not-a-real-key-1234567890'})
    assert b'dummy-secret-key' not in memory.path.read_bytes()
    memory.delete_profile('candidate@example.com')
    assert memory.profile('candidate@example.com') == {}
    assert b'Example City' not in memory.path.read_bytes()


def test_remembering_profile_does_not_erase_details_left_blank(tmp_path):
    memory = AnswerMemory(tmp_path)
    memory.save_profile('candidate@example.com', {'current_city': 'Example City', 'phone': '12345'})
    memory.remember_profile('candidate@example.com', {'current_city': '', 'phone': '67890'})
    assert memory.profile('candidate@example.com') == {'current_city': 'Example City', 'phone': '67890'}


def test_context_dependent_answers_do_not_leak_to_other_jobs_or_locations(tmp_path):
    memory = AnswerMemory(tmp_path)
    question = {'label': 'Are you willing to work in this role?', 'kind': 'choice', 'options': ['Yes', 'No']}
    memory.record_questions('candidate@example.com', job(), [question])
    stored = memory.questions('candidate@example.com')[0]
    assert not stored['can_reuse_globally']
    with pytest.raises(ValueError):
        memory.save_answer('candidate@example.com', stored['id'], 'Yes', reuse_globally=True)
    memory.save_answer('candidate@example.com', stored['id'], 'Yes', reuse_globally=False)
    assert memory.lookup('candidate@example.com', job(), question) == 'Yes'
    assert memory.lookup('candidate@example.com', job(source_id='456'), question) is None
    assert memory.lookup('candidate@example.com', job(location='Mumbai, India'), question) is None
    work_question = {'label': 'Are you authorized to work in the country in which you are applying?', 'kind': 'choice', 'options': ['Yes', 'No']}
    memory.record_questions('candidate@example.com', job(), [work_question])
    work_id = next(item['id'] for item in memory.questions('candidate@example.com') if item['label'] == work_question['label'])
    memory.save_answer('candidate@example.com', work_id, 'Yes', reuse_globally=False)
    assert memory.lookup('candidate@example.com', job(source_id='456'), work_question) is None


def test_ambiguous_country_question_is_limited_to_the_specific_job(tmp_path):
    memory = AnswerMemory(tmp_path)
    question = {'label': 'Country', 'kind': 'choice', 'options': ['India', 'Canada']}
    memory.record_questions('candidate@example.com', job(), [question])
    stored = memory.questions('candidate@example.com')[0]
    assert not stored['can_reuse_globally']
    memory.save_answer('candidate@example.com', stored['id'], 'India', reuse_globally=False)
    assert memory.lookup('candidate@example.com', job(), question) == 'India'
    assert memory.lookup('candidate@example.com', job(source_id='456'), question) is None


def test_known_answers_can_be_added_without_storing_secrets_or_agreements(tmp_path):
    memory = AnswerMemory(tmp_path)
    memory.add_answer('candidate@example.com', 'LinkedIn profile', 'text', 'https://example.com/profile')
    assert memory.lookup('candidate@example.com', job(), {'label': 'LinkedIn profile', 'kind': 'text'}) == 'https://example.com/profile'
    with pytest.raises(ValueError):
        memory.save_answer('candidate@example.com', memory.questions('candidate@example.com')[0]['id'], 'https://example.com/profile', reuse_globally=False)
    for label in ('API key', 'Password', 'Passport number', 'Aadhaar', 'Do you agree to the terms and conditions?', 'Privacy Policy', 'Consent', 'Expected salary?', 'Who referred you?',
                  'Are you authorized to work in the country in which you are applying?'):
        with pytest.raises(ValueError):
            memory.add_answer('candidate@example.com', label, 'text', 'example')
    memory.record_questions('candidate@example.com', job(), [{'label': 'API key', 'kind': 'text'}])
    secret_question = next(item for item in memory.questions('candidate@example.com') if item['label'] == 'API key')
    assert secret_question['kind'] == 'manual'
    with pytest.raises(ValueError):
        memory.save_answer('candidate@example.com', secret_question['id'], 'dummy-secret-key', reuse_globally=False)
    with pytest.raises(ValueError):
        memory.add_answer('candidate@example.com', 'Portfolio', 'text', 'sk-test-not-a-real-key-1234567890')
    memory.record_questions('candidate@example.com', job(), [{'label': 'Long question ' * 60, 'kind': 'text'}])
    assert any(item['kind'] == 'manual' and item['label'].endswith('…') for item in memory.questions('candidate@example.com'))
    memory.add_answer('other@example.com', 'LinkedIn profile', 'text', 'https://example.com/other')
    memory.clear('candidate@example.com')
    assert memory.questions('candidate@example.com') == [] and len(memory.questions('other@example.com')) == 1


def test_profile_conflicts_do_not_reuse_an_outdated_question_answer(tmp_path):
    from app.apply import answer_plan

    memory = AnswerMemory(tmp_path)
    question = {'label': 'Location (City)', 'kind': 'choice', 'options': []}
    memory.record_questions('candidate@example.com', job(), [question])
    question_id = memory.questions('candidate@example.com')[0]['id']
    memory.save_answer('candidate@example.com', question_id, 'Example City, Example State, India', reuse_globally=False)
    fields = [{'id': 'candidate-location', 'name': '', 'label': 'Location (City) *', 'type': 'combobox',
               'required': True, 'react_select': True, 'options': []}]
    candidate = {'email': 'candidate@example.com', 'current_city': 'Example City'}
    resolved, plan, unanswered = answer_plan(fields, candidate, job(), memory)
    assert resolved == {0} and plan[0][3] == 'Example City, Example State, India' and not unanswered
    candidate['current_city'] = 'Different City'
    resolved, _, unanswered = answer_plan(fields, candidate, job(), memory)
    assert not resolved and unanswered


def test_saved_phone_country_can_include_the_user_selected_dial_code(tmp_path):
    from app.apply import answer_plan

    memory = AnswerMemory(tmp_path)
    question = {'label': 'Phone country (calling code)', 'kind': 'choice', 'options': ['India +91', 'Canada +1']}
    memory.record_questions('candidate@example.com', job(), [question])
    question_id = memory.questions('candidate@example.com')[0]['id']
    memory.save_answer('candidate@example.com', question_id, 'India +91', reuse_globally=False)
    fields = [{'id': 'country', 'name': '', 'label': 'Phone country (calling code)', 'type': 'combobox',
               'required': True, 'react_select': True, 'options': []}]
    candidate = {'email': 'candidate@example.com', 'phone_country': 'India'}
    resolved, plan, unanswered = answer_plan(fields, candidate, job(), memory)
    assert resolved == {0} and plan[0][3] == 'India +91' and not unanswered
    candidate['phone_country'] = 'Canada'
    resolved, _, unanswered = answer_plan(fields, candidate, job(), memory)
    assert not resolved and unanswered


def test_phone_country_conflicting_with_an_international_number_needs_review(tmp_path):
    from app.apply import answer_plan

    memory = AnswerMemory(tmp_path)
    fields = [{'id': 'country', 'name': '', 'label': 'Phone country (calling code)', 'type': 'select-one',
               'required': True, 'options': ['India +91', 'Canada +1']}]
    candidate = {'email': 'candidate@example.com', 'phone': '+1 555 0100', 'phone_country': 'India +91'}
    resolved, _, unanswered = answer_plan(fields, candidate, job(), memory)
    assert not resolved and unanswered


def test_generic_city_is_not_assumed_to_mean_residence(tmp_path):
    from app.apply import answer_plan

    memory = AnswerMemory(tmp_path)
    candidate = {'email': 'candidate@example.com', 'current_city': 'Example City'}
    field = {'id': 'office_city', 'name': '', 'label': 'Location (City)', 'type': 'text', 'required': True}
    resolved, _, unanswered = answer_plan([field], candidate, job(), memory)
    assert not resolved and unanswered
    field['id'] = 'candidate-location'
    resolved, plan, _ = answer_plan([field], candidate, job(), memory)
    assert resolved == {0} and plan[0][3] == 'Example City'


def test_conflicting_global_answers_are_not_guessed(tmp_path):
    memory = AnswerMemory(tmp_path)
    question = {'label': 'Notice period?', 'kind': 'text', 'options': []}
    memory.record_questions('candidate@example.com', job(board='one'), [question])
    first = memory.questions('candidate@example.com')[0]['id']
    memory.save_answer('candidate@example.com', first, '30 days', reuse_globally=True)
    memory.record_questions('candidate@example.com', job(board='two'), [question])
    second = next(item['id'] for item in memory.questions('candidate@example.com') if item['id'] != first)
    memory.save_answer('candidate@example.com', second, '60 days', reuse_globally=True)
    assert memory.lookup('candidate@example.com', job(board='three'), question) is None
    assert memory.lookup('candidate@example.com', job(board='one'), question) == '30 days'


def test_manual_actions_are_kept_for_each_job(tmp_path):
    memory = AnswerMemory(tmp_path)
    question = {'label': 'Employer verification required', 'kind': 'manual', 'options': []}
    memory.record_questions('candidate@example.com', job(source_id='123'), [question])
    memory.record_questions('candidate@example.com', job(source_id='456'), [question])
    assert len(memory.questions('candidate@example.com')) == 2


def test_deleted_question_ids_cannot_be_reused_for_a_different_question(tmp_path):
    memory = AnswerMemory(tmp_path)
    memory.record_questions('candidate@example.com', job(), [{'label': 'First question?', 'kind': 'text'}])
    old_id = memory.questions('candidate@example.com')[0]['id']
    memory.delete_question('candidate@example.com', old_id)
    memory.record_questions('candidate@example.com', job(), [{'label': 'Different question?', 'kind': 'text'}])
    assert memory.questions('candidate@example.com')[0]['id'] != old_id
    with pytest.raises(ValueError):
        memory.save_answer('candidate@example.com', old_id, 'Old answer', reuse_globally=False)
