import 'package:flutter_test/flutter_test.dart';

import 'package:feohledger_mobile/models/adaptive.dart';

void main() {
  // The row's `actions` (`api/adaptive_workflows.py::_suggestion_actions`):
  // the screen offers Dismiss from `actions.dismiss`, never from the role or
  // the status.
  group('WorkflowSuggestion.fromJson actions', () {
    const role = {'code': 'role_not_permitted', 'message': 'm'};
    const status = {
      'code': 'workflow_suggestion_not_dismissable',
      'message': 'm',
      'params': {'status': 'applied'},
    };

    WorkflowSuggestion row(String rowStatus, [Object? actions = _absent]) =>
        WorkflowSuggestion.fromJson({
          'id': 's1',
          'kind': 'auto_approve_threshold',
          'vendor_name': 'Acme',
          'title': 't',
          'confidence_pct': '95.00',
          'status': rowStatus,
          if (!identical(actions, _absent)) 'actions': actions,
        });

    test('a null verdict allows the dismiss', () {
      final s = row('open', {'dismiss': null});
      expect(s.dismissVerdict.answered, isTrue);
      expect(s.dismissVerdict.allowed, isTrue);
    });

    test('a stale row the server would dismiss is allowed', () {
      // The status check this replaced offered only `open`.
      expect(row('stale', {'dismiss': null}).dismissVerdict.allowed, isTrue);
    });

    test('a role refusal is refused, with its code', () {
      final v = row('open', {'dismiss': role}).dismissVerdict;
      expect(v.allowed, isFalse);
      expect(v.code, 'role_not_permitted');
    });

    test('a status refusal is refused, with its code', () {
      final v = row('applied', {'dismiss': status}).dismissVerdict;
      expect(v.allowed, isFalse);
      expect(v.code, 'workflow_suggestion_not_dismissable');
    });

    test('no actions at all fails closed', () {
      final v = row('open').dismissVerdict;
      expect(v.answered, isFalse);
      expect(v.allowed, isFalse);
    });

    test('actions without a dismiss key fails closed', () {
      expect(row('open', <String, dynamic>{}).dismissVerdict.allowed, isFalse);
    });

    test('a non-map actions value fails closed', () {
      expect(row('open', 'yes').dismissVerdict.allowed, isFalse);
    });
  });
}

const Object _absent = Object();
