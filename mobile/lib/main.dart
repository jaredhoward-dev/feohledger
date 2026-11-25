import 'dart:async';

import 'package:material_ui/material_ui.dart';
import 'package:flutter/scheduler.dart';

import 'package:feohledger_mobile/l10n/app_localizations_delegates.dart';
import 'package:feohledger_mobile/l10n/gen/app_localizations.dart';
import 'package:feohledger_mobile/screens/home_screen.dart';
import 'package:feohledger_mobile/screens/invoice_detail_screen.dart';
import 'package:feohledger_mobile/screens/legal_accept_screen.dart';
import 'package:feohledger_mobile/screens/login_screen.dart';
import 'package:feohledger_mobile/services/biometric_service.dart';
import 'package:feohledger_mobile/services/push_service.dart';
import 'package:feohledger_mobile/stores/auth_store.dart';
import 'package:feohledger_mobile/stores/locale_store.dart';
import 'package:feohledger_mobile/utils/format_locale.dart';
import 'package:feohledger_mobile/widgets/brand_mark.dart';

Future<void> main() async {
  WidgetsFlutterBinding.ensureInitialized();
  // Load the device's persisted display-language choice before first build so
  // MaterialApp.locale is correct on the very first frame.
  await LocaleStore.instance.init();
  runApp(const APApp());
}

/// The app's one theme.
///
/// A function rather than an inline literal so the native launch screens'
/// copies of its background colour can be checked against it
/// (test/screens/launch_screen_parity_test.dart).
ThemeData buildAppTheme() => ThemeData(
      colorScheme: ColorScheme.fromSeed(seedColor: Colors.blue),
      useMaterial3: true,
      appBarTheme: const AppBarTheme(
        centerTitle: false,
        elevation: 0,
      ),
    );

class APApp extends StatelessWidget {
  const APApp({super.key});

  @override
  Widget build(BuildContext context) {
    // Rebuild MaterialApp whenever the device locale choice changes so the
    // whole tree re-localizes live (no restart). `locale: null` follows the
    // system locale (resolved against supportedLocales).
    return ListenableBuilder(
      listenable: LocaleStore.instance,
      builder: (context, _) {
        return MaterialApp(
          title: 'FeohLedger',
          debugShowCheckedModeBanner: false,
          navigatorKey: PushService.navigatorKey,
          locale: LocaleStore.instance.locale,
          localizationsDelegates: appLocalizationsDelegates,
          supportedLocales: AppLocalizations.supportedLocales,
          // Words and figures move together: this points `utils/money.dart`
          // and `utils/dates.dart` at the locale MaterialApp just resolved, so
          // the picker changes `1,234.50` / `Mar 4, 2026` as well as the copy
          // around them. Under `Localizations`, and above everything that
          // formats.
          builder: (context, child) =>
              FormatLocaleScope(child: child ?? const SizedBox.shrink()),
          theme: buildAppTheme(),
          home: const AuthGate(),
        );
      },
    );
  }
}

/// The app's root route: **a function of auth state**, not a one-shot
/// navigation decision.
///
/// A forced logout can be raised from anywhere — `ApiClient` tears the session
/// down on a 401 from any verb, and `AuthStore.reset()` notifies. When the
/// only route to the home screen was an imperative `pushReplacement` fired once
/// at startup, nothing above `HomeScreen` was listening: a 401 left the user
/// sitting on a home screen with no user, every tab erroring and no way back to
/// login except quitting the app. Rendering the root from
/// `AuthStore.instance.loggedIn` makes "401 responses auto-clear session **and
/// return to login**" true by construction.
class AuthGate extends StatefulWidget {
  const AuthGate({super.key});

  @override
  State<AuthGate> createState() => _AuthGateState();
}

class _AuthGateState extends State<AuthGate> with WidgetsBindingObserver {
  /// True until the one-time startup work (push init, session restore,
  /// biometric unlock) has finished; the splash shows meanwhile.
  bool _booting = true;

  /// A restored session that failed the device biometric check. The credentials
  /// are deliberately kept (a failed Face ID says nothing about the token, and
  /// clearing would wipe the offline cache), but the app stays on the login
  /// screen until the user authenticates again.
  bool _biometricLocked = false;

  /// Last observed sign-in state, so a `loggedIn -> signed out` transition can
  /// be told apart from the notifier's other emissions (loading flags).
  bool _wasLoggedIn = false;

  /// Last observed `legalAcceptanceRequired`, so a resume refresh that newly
  /// requires acceptance can clear any pushed routes off the accept screen.
  bool _wasLegalRequired = false;

  @override
  void initState() {
    super.initState();
    WidgetsBinding.instance.addObserver(this);
    AuthStore.instance.addListener(_onAuthChanged);
    PushService.instance.pendingInvoiceLink.addListener(_openPendingDeepLink);
    _boot();
  }

  @override
  void dispose() {
    WidgetsBinding.instance.removeObserver(this);
    AuthStore.instance.removeListener(_onAuthChanged);
    PushService.instance.pendingInvoiceLink
        .removeListener(_openPendingDeepLink);
    super.dispose();
  }

  /// The app shell is what [build] renders right now — the same conditions,
  /// in one place, so a deep link can never open where the shell could not.
  bool get _shellReachable =>
      !_booting &&
      !_biometricLocked &&
      AuthStore.instance.loggedIn &&
      !AuthStore.instance.legalAcceptanceRequired;

  /// Open a notification's deep link once the shell is reachable. Every
  /// route-opening entry point from outside the tree (a notification tap,
  /// background or cold start) lands in [PushService.pendingInvoiceLink] and
  /// is opened here, so booting, sign-in, the biometric lock and the Terms /
  /// Privacy accept step (decisions §498) all hold it rather than letting an
  /// approvable invoice open on top of them. Re-run on every change that can
  /// make the shell reachable.
  void _openPendingDeepLink() {
    if (!_shellReachable ||
        PushService.instance.pendingInvoiceLink.value == null) {
      return;
    }
    // Deferred to the end of the frame: this can run inside
    // notifyListeners(), mid-build, where pushing a route asserts.
    SchedulerBinding.instance.addPostFrameCallback((_) {
      if (!mounted || !_shellReachable) return;
      final invoiceId = PushService.instance.takePendingInvoiceLink();
      if (invoiceId == null) return;
      Navigator.of(context).push(
        MaterialPageRoute(
          builder: (_) => InvoiceDetailScreen(invoiceId: invoiceId),
        ),
      );
    });
    SchedulerBinding.instance.ensureVisualUpdate();
  }

  /// Back in the foreground: re-read the profile, so a Terms / Privacy version
  /// bump while the app sat in the background puts the accept step in front
  /// of the shell (decisions §498).
  @override
  void didChangeAppLifecycleState(AppLifecycleState state) {
    if (state != AppLifecycleState.resumed || _booting) return;
    if (!AuthStore.instance.loggedIn) return;
    unawaited(AuthStore.instance.refreshProfile());
  }

  Future<void> _boot() async {
    // Initialize push notifications (no-op if Firebase not configured)
    await PushService.instance.init();

    final hasSession = await AuthStore.instance.init();
    var locked = false;
    if (hasSession && await BiometricService.instance.isEnabled) {
      locked = !await BiometricService.instance.authenticate();
    }
    if (!mounted) return;
    setState(() {
      _booting = false;
      _biometricLocked = locked;
    });
    _openPendingDeepLink();
  }

  void _onAuthChanged() {
    final loggedIn = AuthStore.instance.loggedIn;
    final signedOut = _wasLoggedIn && !loggedIn;
    final signedIn = !_wasLoggedIn && loggedIn;
    _wasLoggedIn = loggedIn;
    final legalRequired = AuthStore.instance.legalAcceptanceRequired;
    final newlyRequired = loggedIn && legalRequired && !_wasLegalRequired;
    final legalCleared = loggedIn && _wasLegalRequired && !legalRequired;
    _wasLegalRequired = legalRequired;
    // Push init ran before any session existed, so its device registration
    // could not authenticate; send it now that one does. The server refuses
    // it while the Terms / Privacy are owed (decisions §505), so a sign-in
    // that meets the accept step registers when the step clears instead.
    if ((signedIn && !legalRequired) || legalCleared) {
      unawaited(PushService.instance.registerWithBackend());
    }
    // A link held for a session that has just ended is not the next one's.
    if (signedOut) PushService.instance.takePendingInvoiceLink();
    // Signing in, or accepting the Terms / Privacy, can make the shell
    // reachable; a held deep link opens then.
    _openPendingDeepLink();
    if (!signedOut && !newlyRequired) return;
    // The gate itself is the FIRST route, so a session that ends (or meets the
    // Terms / Privacy accept step) while the user is inside a pushed route
    // (invoice detail, admin, a modal) would leave the login or accept screen
    // rendered underneath and invisible. Drop everything above it.
    // Deferred to the end of the frame: this runs inside notifyListeners(),
    // which can fire mid-build.
    SchedulerBinding.instance.addPostFrameCallback((_) {
      if (!mounted) return;
      Navigator.maybeOf(context)?.popUntil((route) => route.isFirst);
    });
  }

  /// A successful sign-in through the gate's own login screen clears the
  /// biometric lock — the user has just proven who they are with credentials.
  void _onSignedIn() {
    if (!_biometricLocked) return;
    setState(() => _biometricLocked = false);
    _openPendingDeepLink();
  }

  @override
  Widget build(BuildContext context) {
    return ListenableBuilder(
      listenable: AuthStore.instance,
      builder: (context, _) {
        if (_booting) return const SplashScreen();
        if (AuthStore.instance.loggedIn && !_biometricLocked) {
          // Nothing of the app is reachable until the Terms / Privacy in
          // force are accepted — the screen's only other exit is sign-out.
          if (AuthStore.instance.legalAcceptanceRequired) {
            return const LegalAcceptScreen();
          }
          return const HomeScreen();
        }
        return LoginScreen(onSignedIn: _onSignedIn);
      },
    );
  }
}

/// Startup placeholder. Purely visual — the boot sequence and the routing
/// decision both live in [AuthGate].
class SplashScreen extends StatelessWidget {
  const SplashScreen({super.key});

  @override
  Widget build(BuildContext context) {
    return const Scaffold(
      body: Center(
        child: Column(
          mainAxisSize: MainAxisSize.min,
          children: [
            BrandMark(),
            SizedBox(height: 16),
            CircularProgressIndicator(),
          ],
        ),
      ),
    );
  }
}
