/// Where the signed-in user's workspace lives on the web, built the way the
/// backend builds every tenant link (`backend/app/utils/tenant_urls.py::
/// tenant_base_url`): `{slug}` in the deployment's tenant URL template is
/// replaced by the tenant slug, and a template with no `{slug}` is used as is.
///
/// The template is the platform's own (`FEOH_TENANT_URL_TEMPLATE`, published
/// by `GET /api/public-config` — the same value the web signup page reads), so
/// a tenant that set a vanity `tenant_url_template` of its own still gets its
/// platform subdomain here, which serves the same workspace (decisions §503).
///
/// Null when nothing safe can be built: no template, a slug that is missing or
/// not slug-shaped (`backend/app/utils/slug.py::validate_slug_format`, so no
/// typed value can change the host beyond its own label), or a result that
/// is not an absolute `https` URL — the same rule `ExternalLink.safeUri`
/// applies, so the app never launches `http://` or a malformed value.
library;

final _slugPattern = RegExp(r'^[a-z][a-z0-9-]{2,29}$');

String? tenantWebBaseUrl(String? template, String? slug) {
  var base = (template ?? '').trim();
  if (base.isEmpty) return null;
  if (base.contains('{slug}')) {
    final s = (slug ?? '').trim().toLowerCase();
    if (!_slugPattern.hasMatch(s)) return null;
    base = base.replaceAll('{slug}', s);
  }
  while (base.endsWith('/')) {
    base = base.substring(0, base.length - 1);
  }
  final uri = Uri.tryParse(base);
  if (uri == null || uri.scheme != 'https' || uri.host.isEmpty) return null;
  return base;
}

/// The web help centre (`frontend/src/routes/help/`) for that workspace.
String? tenantHelpUrl(String? template, String? slug) {
  final base = tenantWebBaseUrl(template, slug);
  return base == null ? null : '$base/help';
}
