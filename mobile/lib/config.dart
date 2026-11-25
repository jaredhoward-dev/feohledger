class AppConfig {
  static const String defaultApiUrl = 'http://localhost:8000';
  static const String apiPrefix = '/api';

  // Set via login screen or env
  static String apiBaseUrl = defaultApiUrl;
  static String? tenantSlug;

  static String get apiUrl => '$apiBaseUrl$apiPrefix';

  /// Origin of the public web site that publishes the legal pages
  /// (`frontend/src/routes/legal/`). The accept screen links the Terms and the
  /// Privacy Policy there, opened in the browser — the app holds no copy of
  /// either text. Override per build with
  /// `--dart-define=FEOH_PUBLIC_WEB_URL=https://...` (it must be https:
  /// `ExternalLink` renders anything else as plain text).
  static const String publicWebUrl = String.fromEnvironment(
    'FEOH_PUBLIC_WEB_URL',
    defaultValue: 'https://feohledger.com',
  );

  static String get termsUrl => '$publicWebUrl/legal/terms';
  static String get privacyUrl => '$publicWebUrl/legal/privacy';
}
