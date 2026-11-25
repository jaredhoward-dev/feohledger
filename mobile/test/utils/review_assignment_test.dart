import 'package:flutter_test/flutter_test.dart';

import 'package:feohledger_mobile/models/invoice.dart';
import 'package:feohledger_mobile/utils/review_assignment.dart';

/// Mirrors `frontend/src/lib/utils/reviewAssignment.test.ts` (decisions §564).
void main() {
  Invoice invoice({String? id, String? name}) => Invoice.fromJson({
    'id': 'i1',
    'status': 'ready_for_review',
    'created_at': '2026-01-01T12:00:00',
    'assigned_to_id': id,
    'assigned_to': name,
  });

  test('names a colleague the invoice is assigned to', () {
    final a = assignedElsewhere(invoice(id: 'u2', name: ' Pat Lee '), 'u1');
    expect(a, isNotNull);
    expect(a!.name, 'Pat Lee');
  });

  test('an id with no stored name is still someone else', () {
    expect(assignedElsewhere(invoice(id: 'u2'), 'u1')!.name, isNull);
    expect(assignedElsewhere(invoice(id: 'u2', name: '  '), 'u1')!.name, isNull);
  });

  test('null when unassigned or assigned to the reader', () {
    expect(assignedElsewhere(invoice(), 'u1'), isNull);
    expect(assignedElsewhere(invoice(id: ''), 'u1'), isNull);
    expect(assignedElsewhere(invoice(id: 'u1', name: 'Me'), 'u1'), isNull);
  });

  test('a signed-out reader treats any assignee as someone else', () {
    expect(assignedElsewhere(invoice(id: 'u2', name: 'Pat'), null)!.name, 'Pat');
  });
}
