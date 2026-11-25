// Regression coverage for the forced-logout dead end.
//
// `main.dart` held the only route to HomeScreen in an imperative
// `pushReplacement` fired once from the splash screen, and nothing above
// HomeScreen listened to AuthStore. So when `ApiClient` tore the session down
// on a 401 (expired / revoked token) the tree never re-routed: the user sat on
// a home screen with no user — nav collapsed to clerk-level tabs, every tab
// erroring, Settings reading "Unknown" — with no way back to login short of
// quitting the app. `mobile/CLAUDE.md` § API integration promises "401
// responses auto-clear session **and return to login**"; only the clear half
// was implemented.
//
// The root route is now a function of auth state (`AuthGate`), so these pin
// the routing contract rather than any one screen's behaviour.
import 'dart:convert';

import 'package:material_ui/material_ui.dart';
import 'package:flutter/services.dart';
import 'package:flutter_secure_storage/flutter_secure_storage.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';

import 'package:feohledger_mobile/api/api_client.dart';
import 'package:feohledger_mobile/main.dart';
import 'package:feohledger_mobile/screens/home_screen.dart';
import 'package:feohledger_mobile/screens/invoice_detail_screen.dart';
import 'package:feohledger_mobile/screens/legal_accept_screen.dart';
import 'package:feohledger_mobile/screens/login_screen.dart';
import 'package:feohledger_mobile/services/offline_store.dart';
import 'package:feohledger_mobile/services/push_service.dart';
import 'package:feohledger_mobile/stores/auth_store.dart';
import 'package:feohledger_mobile/stores/dashboard_store.dart';
import 'package:feohledger_mobile/stores/invoice_store.dart';

http.Response _json(Object body, [int status = 200]) => http.Response(
      jsonEncode(body),
      status,
      headers: {'content-type': 'application/json'},
    );

Map<String, dynamic> _dashboardBody() => {
      'total_invoices': 0,
      'total_amount': 0,
      'pipeline': <String, int>{},
      'vendor_spend': <Map<String, dynamic>>[],
      'aging': {'current': 0, 'days_30': 0, 'days_60': 0, 'days_90_plus': 0},
      'monthly_trend': <Map<String, dynamic>>[],
      'upcoming_payments': <Map<String, dynamic>>[],
    };

/// Answers every request the clerk-role home tabs make (dashboard, invoices,
/// contracts) plus the auth profile load.
MockClient _homeClient({bool Function()? legalRequired}) =>
    MockClient((req) async {
      final path = req.url.path;
      Map<String, dynamic> me({required bool required}) => {
            'id': 'u1',
            'email': 'demo@acme.com',
            'full_name': 'Demo User',
            'organization_id': 'org1',
            'roles': ['ap_clerk'],
            'legal_acceptance_required': required,
          };
      if (path == '/api/auth/me') {
        return _json(me(required: legalRequired?.call() ?? false));
      }
      if (path == '/api/auth/legal-acceptance') {
        return _json(me(required: false));
      }
      if (path == '/api/dashboard') return _json(_dashboardBody());
      if (path == '/api/invoices') {
        return _json({'invoices': <Map<String, dynamic>>[]});
      }
      if (path == '/api/contracts') {
        return _json({'items': <Map<String, dynamic>>[]});
      }
      return _json({});
    });

void main() {
  TestWidgetsFlutterBinding.ensureInitialized();
  final messenger =
      TestDefaultBinaryMessengerBinding.instance.defaultBinaryMessenger;

  setUpAll(() async {
    OfflineStore.instance.debugUseMemory();
  });

  setUp(() async {
    AuthStore.instance.reset();
    DashboardStore.instance.reset();
    InvoiceStore.instance.reset();
    await OfflineStore.instance.clear();
    PushService.instance.takePendingInvoiceLink();
    ApiClient().debugConfigure();
    // Settings queries local_auth in initState; keep biometrics "unavailable".
    messenger.setMockMethodCallHandler(
      const MethodChannel('plugins.flutter.io/local_auth'),
      (call) async => call.method == 'getAvailableBiometrics' ? <String>[] : false,
    );
  });

  tearDown(() {
    messenger.setMockMethodCallHandler(
      const MethodChannel('plugins.flutter.io/local_auth'),
      null,
    );
  });

  /// Pumps bounded frames — never pumpAndSettle; the loaders animate forever.
  Future<void> pumpFrames(WidgetTester tester, {int frames = 30}) async {
    for (var i = 0; i < frames; i++) {
      await tester.pump(const Duration(milliseconds: 50));
    }
  }

  testWidgets('with no stored session the gate renders the login screen',
      (tester) async {
    FlutterSecureStorage.setMockInitialValues({});
    ApiClient().debugConfigure(client: _homeClient());

    await tester.pumpWidget(const APApp());
    await pumpFrames(tester);

    expect(find.byType(LoginScreen), findsOneWidget);
    expect(find.byType(HomeScreen), findsNothing);
  });

  testWidgets('a restored session renders the home screen', (tester) async {
    FlutterSecureStorage.setMockInitialValues({
      'auth_token': 'tok',
      'tenant_slug': 'acme',
    });
    ApiClient().debugConfigure(client: _homeClient());

    await tester.pumpWidget(const APApp());
    await pumpFrames(tester);

    expect(find.byType(HomeScreen), findsOneWidget);
    expect(find.byType(LoginScreen), findsNothing);
  });

  testWidgets('a 401 forced logout returns to login instead of stranding the '
      'user on the home screen', (tester) async {
    FlutterSecureStorage.setMockInitialValues({
      'auth_token': 'tok',
      'tenant_slug': 'acme',
    });
    ApiClient().debugConfigure(client: _homeClient());

    await tester.pumpWidget(const APApp());
    await pumpFrames(tester);
    expect(find.byType(HomeScreen), findsOneWidget);

    // The token is revoked server-side; the next request 401s.
    ApiClient().debugConfigure(
      client: MockClient((req) async => _json({'detail': 'nope'}, 401)),
      timeout: const Duration(milliseconds: 50),
    );
    await ApiClient().setToken('tok');
    await expectLater(
      ApiClient().get('/invoices'),
      throwsA(isA<ApiException>()),
    );
    await pumpFrames(tester);

    expect(find.byType(LoginScreen), findsOneWidget);
    expect(find.byType(HomeScreen), findsNothing);
  });

  testWidgets('a forced logout also pops routes pushed above the gate',
      (tester) async {
    FlutterSecureStorage.setMockInitialValues({
      'auth_token': 'tok',
      'tenant_slug': 'acme',
    });
    ApiClient().debugConfigure(client: _homeClient());

    await tester.pumpWidget(const APApp());
    await pumpFrames(tester);
    expect(find.byType(HomeScreen), findsOneWidget);

    // The user drills into a detail route (the gate is the FIRST route, so a
    // login screen rendered underneath would otherwise stay invisible).
    PushService.navigatorKey.currentState!.push(
      MaterialPageRoute(
        builder: (_) => const Scaffold(body: Text('pushed detail')),
      ),
    );
    await pumpFrames(tester, frames: 10);
    expect(find.text('pushed detail'), findsOneWidget);

    ApiClient().debugConfigure(
      client: MockClient((req) async => _json({'detail': 'nope'}, 401)),
      timeout: const Duration(milliseconds: 50),
    );
    await ApiClient().setToken('tok');
    await expectLater(
      ApiClient().get('/invoices'),
      throwsA(isA<ApiException>()),
    );
    await pumpFrames(tester);

    expect(find.text('pushed detail'), findsNothing);
    expect(find.byType(LoginScreen), findsOneWidget);
  });

  // Decisions §498: nothing of the app is reachable until the Terms / Privacy
  // in force are accepted.
  testWidgets('a session whose /me requires acceptance renders the accept '
      'screen, not the shell', (tester) async {
    FlutterSecureStorage.setMockInitialValues({
      'auth_token': 'tok',
      'tenant_slug': 'acme',
    });
    ApiClient().debugConfigure(client: _homeClient(legalRequired: () => true));

    await tester.pumpWidget(const APApp());
    await pumpFrames(tester);

    expect(find.byType(LegalAcceptScreen), findsOneWidget);
    expect(find.byType(HomeScreen), findsNothing);
  });

  testWidgets('a requirement raised while backgrounded meets the user on '
      'resume, above any pushed route', (tester) async {
    FlutterSecureStorage.setMockInitialValues({
      'auth_token': 'tok',
      'tenant_slug': 'acme',
    });
    var required = false;
    ApiClient().debugConfigure(
      client: _homeClient(legalRequired: () => required),
    );

    await tester.pumpWidget(const APApp());
    await pumpFrames(tester);
    expect(find.byType(HomeScreen), findsOneWidget);

    PushService.navigatorKey.currentState!.push(
      MaterialPageRoute(
        builder: (_) => const Scaffold(body: Text('pushed detail')),
      ),
    );
    await pumpFrames(tester, frames: 10);

    required = true; // the Terms were bumped while the app was away
    tester.binding.handleAppLifecycleStateChanged(AppLifecycleState.inactive);
    tester.binding.handleAppLifecycleStateChanged(AppLifecycleState.resumed);
    await pumpFrames(tester);

    expect(find.text('pushed detail'), findsNothing);
    expect(find.byType(LegalAcceptScreen), findsOneWidget);
    expect(find.byType(HomeScreen), findsNothing);
  });

  // A notification tap is an entry point from outside the tree. It used to
  // push the invoice detail straight onto the root navigator, so a user who
  // had not accepted the Terms / Privacy in force could open — and approve —
  // an invoice on top of the accept step (decisions §498). The gate now holds
  // every deep link until the shell is reachable.
  group('notification deep links', () {
    testWidgets('a tap on the shell opens the invoice', (tester) async {
      FlutterSecureStorage.setMockInitialValues({
        'auth_token': 'tok',
        'tenant_slug': 'acme',
      });
      ApiClient().debugConfigure(client: _homeClient());

      await tester.pumpWidget(const APApp());
      await pumpFrames(tester);
      expect(find.byType(HomeScreen), findsOneWidget);

      PushService.instance.openFromNotification({'invoice_id': 'inv-1'});
      await pumpFrames(tester, frames: 10);

      expect(find.byType(InvoiceDetailScreen), findsOneWidget);
      expect(PushService.instance.pendingInvoiceLink.value, isNull);
    });

    testWidgets('a tap while acceptance is required is held behind the accept '
        'step, then opens once accepted', (tester) async {
      FlutterSecureStorage.setMockInitialValues({
        'auth_token': 'tok',
        'tenant_slug': 'acme',
      });
      var required = true;
      ApiClient().debugConfigure(
        client: _homeClient(legalRequired: () => required),
      );

      // Delivered during boot, as a cold-start tap is.
      PushService.instance.openFromNotification({'invoice_id': 'inv-1'});
      await tester.pumpWidget(const APApp());
      await pumpFrames(tester);

      expect(find.byType(LegalAcceptScreen), findsOneWidget);
      expect(find.byType(InvoiceDetailScreen), findsNothing);

      // And one delivered while the accept step is already showing.
      PushService.instance.openFromNotification({'invoice_id': 'inv-1'});
      await pumpFrames(tester, frames: 10);
      expect(find.byType(InvoiceDetailScreen), findsNothing);
      expect(find.byType(LegalAcceptScreen), findsOneWidget);

      required = false;
      expect(await AuthStore.instance.acceptLegalDocuments(),
          LegalAcceptOutcome.accepted);
      await pumpFrames(tester);

      expect(find.byType(InvoiceDetailScreen), findsOneWidget);
      expect(find.byType(LegalAcceptScreen), findsNothing);
    });

    testWidgets('a requirement raised on resume holds a later tap',
        (tester) async {
      FlutterSecureStorage.setMockInitialValues({
        'auth_token': 'tok',
        'tenant_slug': 'acme',
      });
      var required = false;
      ApiClient().debugConfigure(
        client: _homeClient(legalRequired: () => required),
      );

      await tester.pumpWidget(const APApp());
      await pumpFrames(tester);
      expect(find.byType(HomeScreen), findsOneWidget);

      required = true;
      tester.binding.handleAppLifecycleStateChanged(AppLifecycleState.inactive);
      tester.binding.handleAppLifecycleStateChanged(AppLifecycleState.resumed);
      await pumpFrames(tester);
      expect(find.byType(LegalAcceptScreen), findsOneWidget);

      PushService.instance.openFromNotification({'invoice_id': 'inv-1'});
      await pumpFrames(tester, frames: 10);

      expect(find.byType(InvoiceDetailScreen), findsNothing);
      expect(find.byType(LegalAcceptScreen), findsOneWidget);
    });

    testWidgets('a tap while signed out does not open over the login screen',
        (tester) async {
      FlutterSecureStorage.setMockInitialValues({});
      ApiClient().debugConfigure(client: _homeClient());

      await tester.pumpWidget(const APApp());
      await pumpFrames(tester);
      expect(find.byType(LoginScreen), findsOneWidget);

      PushService.instance.openFromNotification({'invoice_id': 'inv-1'});
      await pumpFrames(tester, frames: 10);

      expect(find.byType(InvoiceDetailScreen), findsNothing);
      expect(find.byType(LoginScreen), findsOneWidget);
    });

    test('a notification without an invoice id holds nothing', () {
      PushService.instance.openFromNotification({'type': 'digest'});
      expect(PushService.instance.pendingInvoiceLink.value, isNull);
    });
  });
}
