import 'dart:async';
import 'dart:io' show Platform;

import 'package:firebase_messaging/firebase_messaging.dart';
import 'package:material_ui/material_ui.dart';
import 'package:flutter_local_notifications/flutter_local_notifications.dart';

import 'package:feohledger_mobile/api/endpoints.dart';

/// Push notification service — Firebase Cloud Messaging.
///
/// Requires Firebase project setup:
/// - iOS: GoogleService-Info.plist in ios/Runner/
/// - Android: google-services.json in android/app/
///
/// Until Firebase is configured, all methods are no-ops that log warnings.
class PushService {
  static final PushService instance = PushService._();
  PushService._();

  /// Root navigator key, wired into `MaterialApp.navigatorKey` in main.dart.
  static final GlobalKey<NavigatorState> navigatorKey = GlobalKey<NavigatorState>();

  /// The invoice a tapped notification asked to open, held until the root
  /// `AuthGate` can show it.
  ///
  /// This service never pushes a route itself. A tap can arrive while the app
  /// is booting, signed out, biometric-locked or on the blocking Terms /
  /// Privacy accept step (decisions §498) — pushing an invoice detail from
  /// here put an approvable invoice on top of that step, and the server does
  /// not gate on the flag. The gate is the one place that knows whether the
  /// app shell is reachable, so it opens the link then, and only then.
  final ValueNotifier<String?> pendingInvoiceLink = ValueNotifier<String?>(null);

  /// Record the deep link a notification's [data] carries (`invoice_id`).
  /// The FCM tap handlers call this; tests call it directly, since a unit test
  /// cannot build a `RemoteMessage` delivery.
  void openFromNotification(Map<String, dynamic> data) {
    final invoiceId = data['invoice_id'];
    if (invoiceId is! String || invoiceId.isEmpty) {
      debugPrint('[push] Tap carried no invoice_id — nothing to navigate to');
      return;
    }
    pendingInvoiceLink.value = invoiceId;
  }

  /// The gate takes the link as it opens it, so it opens once.
  String? takePendingInvoiceLink() {
    final id = pendingInvoiceLink.value;
    pendingInvoiceLink.value = null;
    return id;
  }

  bool _initialized = false;
  String? _fcmToken;

  String? get fcmToken => _fcmToken;

  /// Test seam: stands in for an `onRegistered` emission without a Firebase
  /// app, which a unit test cannot initialize.
  @visibleForTesting
  void debugSetRegistration(String? id) => _fcmToken = id;

  /// Sends this device's current FCM registration to the backend, if FCM has
  /// issued one. The auth gate calls this on every sign-in (fresh login or a
  /// restored session): the registration attempted during [init] runs before
  /// any session exists, so without this a device would only ever reach the
  /// backend if its identifier happened to change while signed in.
  Future<void> registerWithBackend() async {
    final id = _fcmToken;
    if (id == null) return;
    await _registerToken(id);
  }

  final _localNotifications = FlutterLocalNotificationsPlugin();

  /// Initialize FCM and request permissions.
  /// Call once from main.dart after Firebase.initializeApp().
  Future<void> init() async {
    if (_initialized) return;

    try {
      // Request permission (iOS prompts user, Android auto-grants)
      final settings = await FirebaseMessaging.instance.requestPermission(
        alert: true,
        badge: true,
        sound: true,
      );

      if (settings.authorizationStatus == AuthorizationStatus.denied) {
        debugPrint('[push] Notification permission denied');
        return;
      }

      // Register this app instance with FCM. Since firebase_messaging 17 the
      // device's push identity is its Firebase Installation ID (FID), delivered
      // on `onRegistered` — on the first registration and again whenever it
      // changes — replacing the deprecated `getToken()` + `onTokenRefresh`
      // pair. Subscribe BEFORE calling `register()` so the first FID cannot be
      // emitted into a stream nobody is listening to yet.
      FirebaseMessaging.instance.onRegistered.listen(_onRegistered);
      await FirebaseMessaging.instance.register();

      // Initialize local notifications for foreground display. The small icon
      // is a white silhouette: Android draws it from alpha alone, so the
      // launcher tile would show as a solid white square.
      await _localNotifications.initialize(
        settings: const InitializationSettings(
          android: AndroidInitializationSettings('@drawable/ic_notification'),
          iOS: DarwinInitializationSettings(),
        ),
      );

      // Handle foreground messages
      FirebaseMessaging.onMessage.listen(_handleForegroundMessage);

      // Handle background message taps (app was in background)
      FirebaseMessaging.onMessageOpenedApp.listen(_handleMessageTap);

      // A tap that launched the app from terminated never reaches
      // onMessageOpenedApp; it is delivered once here. Held like any other
      // until the gate can open it.
      final initial = await FirebaseMessaging.instance.getInitialMessage();
      if (initial != null) _handleMessageTap(initial);

      _initialized = true;
      debugPrint('[push] Push notifications initialized');
    } catch (e) {
      debugPrint('[push] Init failed (Firebase not configured?): $e');
    }
  }

  void _onRegistered(String fid) {
    _fcmToken = fid;
    debugPrint('[push] FCM registration updated');
    unawaited(_registerToken(fid));
  }

  void _handleForegroundMessage(RemoteMessage message) {
    debugPrint('[push] Foreground message: ${message.notification?.title}');

    final notification = message.notification;
    if (notification == null) return;

    // Show local notification since FCM doesn't auto-show in foreground
    _localNotifications.show(
      id: notification.hashCode,
      title: notification.title,
      body: notification.body,
      notificationDetails: const NotificationDetails(
        android: AndroidNotificationDetails(
          'feoh_approvals',
          'Approval Notifications',
          channelDescription: 'Invoice approval requests and status updates',
          importance: Importance.high,
          priority: Priority.high,
        ),
        iOS: DarwinNotificationDetails(),
      ),
    );
  }

  void _handleMessageTap(RemoteMessage message) {
    debugPrint('[push] Message tapped');
    openFromNotification(message.data);
  }

  /// Best-effort registration of [token] for this device's [platform] with
  /// the backend (`POST /api/notifications/device-token`). Mirrors the
  /// try/catch-and-log discipline of the rest of this service — a failure
  /// here (no network, not yet authenticated) must never crash or block push
  /// setup. Before login it fails (the caller has no JWT yet) — and push init
  /// runs before session restore, so on a cold start it always does. The
  /// `onRegistered` stream does not help there: it fires only when the
  /// identifier changes, not on every launch. [registerWithBackend] is the
  /// retry, called by the auth gate on every sign-in.
  Future<void> _registerToken(String token) async {
    final platform = Platform.isIOS ? 'ios' : 'android';
    try {
      await NotificationApi.registerDeviceToken(token, platform);
      debugPrint('[push] Device token registered ($platform)');
    } catch (e) {
      debugPrint('[push] Device token registration failed (not logged in yet?): $e');
    }
  }
}
