#!/usr/bin/env bash
# Check that each origin serves THIS build of the SPA, the way CloudFront is
# meant to serve it (infra/frontend.tf). Run by .github/workflows/vm-deploy.yml
# right after deploy/publish-frontend.sh; safe to run by hand.
#
#   deploy/smoke-frontend.sh frontend/build https://d123.cloudfront.net [https://feohledger.com …]
#
# For every origin:
#   - /_app/version.json is byte-for-byte the build's own (so the edge is not
#     serving the previous release from cache);
#   - /, a reload of a real tenant route with its URL state
#     (/invoices?status=…), and a deep link whose last segment contains a dot
#     all return the SPA's index.html with 200 — the dotted segment is the case
#     an "extensionless paths only" fallback rule gets wrong
#     (infra/cloudfront/spa-router.js), the query string the case a rewrite
#     that folds it into the path gets wrong;
#   - a missing hashed asset is a 404 that is NOT the SPA shell: neither
#     index.html with a 200 nor index.html under a 404 status (what a
#     custom_error_response mapping 404 to /index.html would serve, and what a
#     browser then parses as a broken script);
#   - HSTS is set.
#
# vm-deploy.yml passes the distribution's own domain, the apex and a tenant
# host (smoke-check.<apex>), so the tenant-route reload runs on a tenant
# subdomain. curl, not Playwright: each check is the status, content type or
# body of one HTTP response, which a browser would only observe less directly
# (docs/decisions.md §589).
set -euo pipefail

die() {
	echo "smoke-frontend: $*" >&2
	exit 1
}

[ $# -ge 2 ] || die "usage: $0 <build-dir> <origin> [<origin> …]"
build=${1%/}
shift
[ -s "$build/_app/version.json" ] || die "$build/_app/version.json is missing"
want_version=$(cat "$build/_app/version.json")

work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT

# fetch <url> → sets status; body in $work/body, headers in $work/headers.
fetch() {
	status=$(curl -sS --max-time 20 -o "$work/body" -D "$work/headers" -w '%{http_code}' "$1") ||
		die "$1: request failed"
}

failures=0
fail() {
	echo "  FAIL $*" >&2
	failures=$((failures + 1))
}

for origin in "$@"; do
	origin=${origin%/}
	echo "==> ${origin}"

	fetch "${origin}/_app/version.json"
	if [ "$status" != 200 ]; then
		fail "/_app/version.json: HTTP ${status}"
	elif [ "$(cat "$work/body")" != "$want_version" ]; then
		fail "/_app/version.json is $(cat "$work/body"), expected ${want_version} — the edge is not serving this build"
	fi

	for path in / '/invoices?status=ready_for_review' /smoke-check/route.with.dots; do
		fetch "${origin}${path}"
		if [ "$status" != 200 ]; then
			fail "${path}: HTTP ${status}, expected the SPA with 200"
		elif ! grep -qi '^content-type: text/html' "$work/headers"; then
			fail "${path}: not served as text/html"
		elif ! cmp -s "$work/body" "$build/index.html"; then
			fail "${path}: did not return this build's index.html"
		fi
		if ! grep -qi '^strict-transport-security: max-age=31536000; includeSubDomains' "$work/headers"; then
			fail "${path}: Strict-Transport-Security missing or changed"
		fi
	done

	fetch "${origin}/_app/immutable/feoh-smoke-missing.js"
	if [ "$status" != 404 ]; then
		fail "a missing hashed asset returned HTTP ${status}, expected 404"
	elif grep -qi '^content-type: text/html' "$work/headers" || cmp -s "$work/body" "$build/index.html"; then
		fail "a missing hashed asset returned 404 with the SPA shell, expected the origin's own not-found (a custom error response mapping 404 to /index.html?)"
	fi
done

[ "$failures" -eq 0 ] || die "${failures} check(s) failed"
echo "==> every origin serves ${want_version}"
