// The blocking Terms / Privacy accept step (decisions §495, §498).
import 'dart:async';
import 'dart:convert';

import 'package:material_ui/material_ui.dart';
import 'package:flutter_secure_storage/flutter_secure_storage.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';

import 'package:feohledger_mobile/api/api_client.dart';
import 'package:feohledger_mobile/config.dart';
import 'package:feohledger_mobile/l10n/app_localizations_delegates.dart';
import 'package:feohledger_mobile/l10n/gen/app_localizations.dart';
import 'package:feohledger_mobile/legal_versions.generated.dart';
import 'package:feohledger_mobile/screens/legal_accept_screen.dart';
import 'package:feohledger_mobile/services/offline_store.dart';
import 'package:feohledger_mobile/stores/auth_store.dart';
import 'package:feohledger_mobile/widgets/external_link.dart';

http.Response _json(Object body, [int status = 200]) => http.Response(
      jsonEncode(body),
      status,
      headers: {'content-type': 'application/json'},
    );

Map<String, dynamic> _me({required bool required}) => {
      'id': 'u1',
      'email': 'demo@acme.com',
      'full_name': 'Demo User',
      'organization_id': 'org1',
      'roles': ['ap_clerk'],
      'legal_acceptance_required': required,
    };

void main() {
  late List<http.Request> accepts;
  late bool loggedOut;

  setUpAll(() => OfflineStore.instance.debugUseMemory());

  /// Signs in a user whose `/me` requires acceptance; [onAccept] answers the
  /// `POST /api/auth/legal-acceptance`.
  Future<void> signIn(
    FutureOr<http.Response> Function() onAccept,
  ) async {
    FlutterSecureStorage.setMockInitialValues({});
    AuthStore.instance.reset();
    accepts = [];
    loggedOut = false;
    ApiClient().debugConfigure(
      client: MockClient((req) async {
        final path = req.url.path;
        if (path == '/api/auth/login') return _json({'access_token': 'tok'});
        if (path == '/api/auth/me') return _json(_me(required: true));
        if (path == '/api/auth/legal-acceptance') {
          accepts.add(req);
          return onAccept();
        }
        if (path == '/api/auth/logout') {
          loggedOut = true;
          return _json({});
        }
        return _json({});
      }),
    );
    await AuthStore.instance.login('demo@acme.com', 'demo', 'acme');
    expect(AuthStore.instance.legalAcceptanceRequired, isTrue);
  }

  Future<void> pump(
    WidgetTester tester, {
    UrlOpener? opener,
    Locale? locale,
    double textScale = 1,
  }) async {
    await tester.pumpWidget(
      MaterialApp(
        locale: locale,
        localizationsDelegates: appLocalizationsDelegates,
        supportedLocales: AppLocalizations.supportedLocales,
        builder: (context, child) => MediaQuery(
          data: MediaQuery.of(context)
              .copyWith(textScaler: TextScaler.linear(textScale)),
          child: child!,
        ),
        home: LegalAcceptScreen(opener: opener),
      ),
    );
    await tester.pump();
  }

  Future<void> meetsEveryGuideline(WidgetTester tester) async {
    await expectLater(tester, meetsGuideline(androidTapTargetGuideline));
    await expectLater(tester, meetsGuideline(iOSTapTargetGuideline));
    await expectLater(tester, meetsGuideline(labeledTapTargetGuideline));
    await expectLater(tester, meetsGuideline(textContrastGuideline));
  }

  /// The notice is announced by `A11y.announce`; a live region on it as well
  /// would read the same text twice.
  Finder liveRegionIn(Finder notice) => find.descendant(
        of: notice,
        matching: find.byWidgetPredicate(
          (w) => w is Semantics && (w.properties.liveRegion ?? false),
        ),
      );

  Finder acceptButton() => find.widgetWithText(FilledButton, 'Accept');

  testWidgets('Accept with the box unticked says to tick it and posts nothing',
      (tester) async {
    await tester.runAsync(() => signIn(() => _json(_me(required: false))));
    await pump(tester);

    await tester.tap(acceptButton());
    await tester.pump();

    expect(
      find.text('Tick the box to accept the Terms of Service and Privacy '
          'Policy.'),
      findsOneWidget,
    );
    expect(accepts, isEmpty);
    expect(AuthStore.instance.legalAcceptanceRequired, isTrue);
  });

  testWidgets('ticked, Accept posts the versions in force and clears the flag',
      (tester) async {
    await tester.runAsync(() => signIn(() => _json(_me(required: false))));
    await pump(tester);

    await tester.tap(find.byKey(const Key('legal-accept-checkbox')));
    await tester.pump();
    await tester.runAsync(() async {
      await tester.tap(acceptButton());
      await Future<void>.delayed(const Duration(milliseconds: 50));
    });
    await tester.pump();

    expect(accepts, hasLength(1));
    expect(jsonDecode(accepts.single.body), {
      'accepted_terms_version': kTermsVersion,
      'accepted_privacy_version': kPrivacyVersion,
    });
    expect(AuthStore.instance.legalAcceptanceRequired, isFalse);
  });

  testWidgets('a coded 422 says the app is out of date and stops re-asking',
      (tester) async {
    await tester.runAsync(
      () => signIn(
        () => _json({
          'detail': {
            'code': 'legal_acceptance_required',
            'message': 'Please accept the current Terms of Service and '
                'Privacy Policy to continue.',
          },
        }, 422),
      ),
    );
    await pump(tester);

    await tester.tap(find.byKey(const Key('legal-accept-checkbox')));
    await tester.pump();
    await tester.runAsync(() async {
      await tester.tap(acceptButton());
      await Future<void>.delayed(const Duration(milliseconds: 50));
    });
    await tester.pump();

    expect(find.byKey(const Key('legal-app-outdated')), findsOneWidget);
    expect(find.textContaining('Update the app'), findsOneWidget);
    // No Accept to press again — the same versions would be refused again.
    expect(acceptButton(), findsNothing);
    expect(find.widgetWithText(TextButton, 'Sign out'), findsOneWidget);
    expect(AuthStore.instance.legalAcceptanceRequired, isTrue);
  });

  testWidgets('a 429 shows the standard error and keeps Accept',
      (tester) async {
    await tester.runAsync(
      () => signIn(() => _json({'detail': 'Too many requests'}, 429)),
    );
    await pump(tester);

    await tester.tap(find.byKey(const Key('legal-accept-checkbox')));
    await tester.pump();
    await tester.runAsync(() async {
      await tester.tap(acceptButton());
      await Future<void>.delayed(const Duration(milliseconds: 50));
    });
    await tester.pump();

    expect(find.byKey(const Key('legal-accept-error')), findsOneWidget);
    expect(find.text('Too many requests'), findsOneWidget);
    expect(acceptButton(), findsOneWidget);
    expect(liveRegionIn(find.byKey(const Key('legal-accept-error'))),
        findsNothing);
  });

  testWidgets('error state: 48dp targets, labels and contrast hold',
      (tester) async {
    final handle = tester.ensureSemantics();
    await tester.runAsync(
      () => signIn(() => _json({'detail': 'Too many requests'}, 429)),
    );
    await pump(tester);

    await tester.tap(find.byKey(const Key('legal-accept-checkbox')));
    await tester.pump();
    await tester.runAsync(() async {
      await tester.tap(acceptButton());
      await Future<void>.delayed(const Duration(milliseconds: 50));
    });
    await tester.pump();
    expect(find.byKey(const Key('legal-accept-error')), findsOneWidget);

    await meetsEveryGuideline(tester);
    handle.dispose();
  });

  testWidgets('busy: the Accept button keeps an accessible name and the '
      'guidelines hold', (tester) async {
    final handle = tester.ensureSemantics();
    final held = Completer<http.Response>();
    await tester.runAsync(() => signIn(() => held.future));
    await pump(tester);

    await tester.tap(find.byKey(const Key('legal-accept-checkbox')));
    await tester.pump();
    await tester.tap(find.byType(FilledButton));
    await tester.pump();
    expect(AuthStore.instance.acceptingLegal, isTrue);

    // Only a spinner is on the button now; its label is the button's name.
    expect(
      tester.getSemantics(find.byType(FilledButton)),
      containsSemantics(label: 'Loading…', isButton: true),
    );
    await meetsEveryGuideline(tester);

    held.complete(_json(_me(required: false)));
    await tester.runAsync(
      () => Future<void>.delayed(const Duration(milliseconds: 50)),
    );
    await tester.pump();
    expect(AuthStore.instance.acceptingLegal, isFalse);
    handle.dispose();
  });

  testWidgets('the out-of-date notice fits at 200% text in German',
      (tester) async {
    tester.view.physicalSize = const Size(390 * 3, 844 * 3);
    tester.view.devicePixelRatio = 3;
    addTearDown(tester.view.reset);
    await tester.runAsync(
      () => signIn(
        () => _json({
          'detail': {
            'code': 'legal_acceptance_required',
            'message': 'outdated',
          },
        }, 422),
      ),
    );
    await pump(tester, locale: const Locale('de'), textScale: 2);
    expect(tester.takeException(), isNull);

    // At this size the column scrolls; bring each control on screen to tap.
    await tester.ensureVisible(find.byKey(const Key('legal-accept-checkbox')));
    await tester.pump();
    await tester.tap(find.byKey(const Key('legal-accept-checkbox')));
    await tester.pump();
    await tester.ensureVisible(find.byType(FilledButton));
    await tester.pump();
    await tester.runAsync(() async {
      await tester.tap(find.byType(FilledButton));
      await Future<void>.delayed(const Duration(milliseconds: 50));
    });
    await tester.pump();

    final notice = find.byKey(const Key('legal-app-outdated'));
    expect(notice, findsOneWidget);
    expect(tester.takeException(), isNull, reason: 'no RenderFlex overflow');
    await tester.scrollUntilVisible(
      find.widgetWithText(TextButton, 'Abmelden'),
      200,
    );
    expect(tester.takeException(), isNull);
    expect(liveRegionIn(notice), findsNothing);
  });

  testWidgets('Sign out ends the session', (tester) async {
    await tester.runAsync(() => signIn(() => _json(_me(required: false))));
    await pump(tester);

    await tester.runAsync(() async {
      await tester.tap(find.widgetWithText(TextButton, 'Sign out'));
      await Future<void>.delayed(const Duration(milliseconds: 50));
    });
    await tester.pump();

    expect(loggedOut, isTrue);
    expect(AuthStore.instance.loggedIn, isFalse);
  });

  testWidgets('the document links open the published web pages',
      (tester) async {
    await tester.runAsync(() => signIn(() => _json(_me(required: false))));
    final opened = <Uri>[];
    await pump(tester, opener: (uri) async {
      opened.add(uri);
      return true;
    });

    await tester.tap(find.byKey(const Key('legal-terms-link')));
    await tester.tap(find.byKey(const Key('legal-privacy-link')));
    await tester.pump();

    expect(opened, [
      Uri.parse(AppConfig.termsUrl),
      Uri.parse(AppConfig.privacyUrl),
    ]);
    expect(opened.every((u) => u.scheme == 'https'), isTrue);
  });

  testWidgets('semantics: labelled checkbox, links, 48dp targets, contrast',
      (tester) async {
    final handle = tester.ensureSemantics();
    await tester.runAsync(() => signIn(() => _json(_me(required: false))));
    await pump(tester);

    // The checkbox is named by the sentence it accepts.
    expect(
      tester.getSemantics(find.byKey(const Key('legal-accept-checkbox'))),
      containsSemantics(
        label: 'I have read and accept the Terms of Service and the Privacy '
            'Policy.',
        hasCheckedState: true,
        isChecked: false,
        hasTapAction: true,
      ),
    );
    // Each document is announced as a link that opens in the browser.
    expect(
      find.bySemanticsLabel('Terms of Service, opens in your browser'),
      findsOneWidget,
    );
    expect(
      tester.getSemantics(
        find.bySemanticsLabel('Privacy Policy, opens in your browser'),
      ),
      containsSemantics(isLink: true, hasTapAction: true),
    );

    await expectLater(tester, meetsGuideline(androidTapTargetGuideline));
    await expectLater(tester, meetsGuideline(iOSTapTargetGuideline));
    await expectLater(tester, meetsGuideline(labeledTapTargetGuideline));
    await expectLater(tester, meetsGuideline(textContrastGuideline));
    handle.dispose();
  });
}
