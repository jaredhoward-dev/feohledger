import 'package:flutter_test/flutter_test.dart';

import 'package:feohledger_mobile/utils/tenant_web_url.dart';

// The mobile half of `backend/app/utils/tenant_urls.py::tenant_base_url`'s
// global-template branch, used by Settings → Help (decisions §503).
void main() {
  group('tenantWebBaseUrl', () {
    test('substitutes the slug into the platform template', () {
      expect(
        tenantWebBaseUrl('https://{slug}.feohledger.com', 'acme'),
        'https://acme.feohledger.com',
      );
    });

    test('drops trailing slashes and lowercases the slug', () {
      expect(
        tenantWebBaseUrl('https://{slug}.app.example.com//', 'Acme'),
        'https://acme.app.example.com',
      );
    });

    test('uses a template with no {slug} as is', () {
      expect(
        tenantWebBaseUrl('https://ap.example.com', null),
        'https://ap.example.com',
      );
    });

    test('is null with no template, or no usable slug', () {
      expect(tenantWebBaseUrl(null, 'acme'), isNull);
      expect(tenantWebBaseUrl('  ', 'acme'), isNull);
      expect(tenantWebBaseUrl('https://{slug}.feohledger.com', null), isNull);
      expect(tenantWebBaseUrl('https://{slug}.feohledger.com', ''), isNull);
    });

    test('refuses a slug that could reshape the host', () {
      for (final slug in ['evil.com/', 'a', 'acme.evil', 'x@y', '1acme']) {
        expect(
          tenantWebBaseUrl('https://{slug}.feohledger.com', slug),
          isNull,
          reason: slug,
        );
      }
    });

    test('launches https only, like ExternalLink', () {
      expect(tenantWebBaseUrl('http://{slug}.localhost:7777', 'acme'), isNull);
      expect(tenantWebBaseUrl('ftp://{slug}.example.com', 'acme'), isNull);
      expect(tenantWebBaseUrl('{slug}.example.com', 'acme'), isNull);
    });
  });

  test('tenantHelpUrl appends /help', () {
    expect(
      tenantHelpUrl('https://{slug}.feohledger.com/', 'acme'),
      'https://acme.feohledger.com/help',
    );
    expect(tenantHelpUrl(null, 'acme'), isNull);
  });
}
