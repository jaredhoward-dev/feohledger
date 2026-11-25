// Tap-navigation of an in-app notification row (`AppNotification`): which
// rows open a detail screen. The `erp_bill_flagged` notice is an `exception`
// row (decisions §539) and opens the flag, as the web does.
import 'package:flutter_test/flutter_test.dart';

import 'package:feohledger_mobile/models/notification.dart';

AppNotification _row(String entityType, String? entityId) =>
    AppNotification.fromJson({
      'id': 'n1',
      'event_type': 'erp_bill_flagged',
      'entity_type': entityType,
      'entity_id': entityId,
      'title': 't',
      'created_at': '2026-11-25T00:00:00Z',
    });

void main() {
  test('an exception row with an id links to the exception, not an invoice',
      () {
    final n = _row('exception', 'e1');
    expect(n.linksToException, isTrue);
    expect(n.linksToInvoice, isFalse);
  });

  test('an exception row without an id links nowhere', () {
    final n = _row('exception', null);
    expect(n.linksToException, isFalse);
    expect(n.linksToInvoice, isFalse);
  });

  test('an invoice row still links to the invoice only', () {
    final n = _row('invoice', 'i1');
    expect(n.linksToInvoice, isTrue);
    expect(n.linksToException, isFalse);
  });

  test('a contract row has no mobile detail', () {
    final n = _row('contract', 'c1');
    expect(n.linksToInvoice, isFalse);
    expect(n.linksToException, isFalse);
  });
}
