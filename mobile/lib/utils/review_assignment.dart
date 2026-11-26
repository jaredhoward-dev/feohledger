import 'package:feohledger_mobile/models/invoice.dart';

/// Whose the invoice is, for the note above the review buttons — never whether
/// the reader may act on it. The mobile twin of the web's
/// `frontend/src/lib/utils/reviewAssignment.ts`.
///
/// Assignment is routing, not authority (decisions §564): approve / reject
/// accept any approver who passes the server's gates, and the screen offers
/// them exactly as `GET /review-actions` says. A reader who is not the assignee
/// still gets the buttons, with a note naming the colleague so they do not
/// step on someone else's queue by accident.
class ReviewAssignee {
  /// The colleague's display name, or `null` when only an id is stored (the
  /// caller then shows a generic label).
  final String? name;

  const ReviewAssignee(this.name);
}

/// The assignee when [invoice] is assigned to someone other than [viewerId],
/// else `null` (unassigned, or assigned to the reader). An id with no stored
/// name still counts as "someone else" (`name: null`).
ReviewAssignee? assignedElsewhere(Invoice invoice, String? viewerId) {
  final assignee = invoice.assignedToId;
  if (assignee == null || assignee.isEmpty || assignee == viewerId) {
    return null;
  }
  final name = invoice.assignedTo?.trim();
  return ReviewAssignee(name == null || name.isEmpty ? null : name);
}
