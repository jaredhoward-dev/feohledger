import 'package:material_ui/material_ui.dart';

import 'package:feohledger_mobile/config.dart';
import 'package:feohledger_mobile/l10n/gen/app_localizations.dart';
import 'package:feohledger_mobile/stores/auth_store.dart';
import 'package:feohledger_mobile/utils/a11y.dart';
import 'package:feohledger_mobile/widgets/brand_mark.dart';
import 'package:feohledger_mobile/widgets/external_link.dart';

/// The blocking Terms / Privacy accept step (decisions §495, §498).
///
/// `AuthGate` renders this instead of the app shell while the signed-in user's
/// `/me` says `legal_acceptance_required`. It offers exactly two ways out:
/// accept the versions in force (the checkbox is required), or sign out. The
/// two documents are linked to the published web pages and open in the
/// browser — the app keeps no copy of either text.
///
/// A 422 refusal means this build carries older versions than the server's;
/// re-asking would send the same versions forever, so the screen says to
/// update the app and stops offering Accept.
class LegalAcceptScreen extends StatefulWidget {
  /// Injected by tests; defaults to the browser ([openExternally]).
  final UrlOpener? opener;

  const LegalAcceptScreen({super.key, this.opener});

  @override
  State<LegalAcceptScreen> createState() => _LegalAcceptScreenState();
}

class _LegalAcceptScreenState extends State<LegalAcceptScreen> {
  bool _ticked = false;

  /// Accept was pressed with the box unticked.
  bool _showTickRequired = false;

  /// The server refused this build's versions (422): the app is out of date.
  bool _appOutdated = false;

  Future<void> _accept() async {
    final l = AppLocalizations.of(context);
    if (!_ticked) {
      setState(() => _showTickRequired = true);
      A11y.announce(context, l.legalAcceptTickRequired);
      return;
    }
    final outcome = await AuthStore.instance.acceptLegalDocuments();
    if (!mounted) return;
    switch (outcome) {
      case LegalAcceptOutcome.accepted:
        // AuthGate swaps this screen for the shell on the store's notify.
        break;
      case LegalAcceptOutcome.appOutdated:
        setState(() => _appOutdated = true);
        A11y.announce(context, l.legalAcceptAppOutdated);
      case LegalAcceptOutcome.failed:
        final message = AuthStore.instance.legalError;
        if (message != null) A11y.announce(context, message);
    }
  }

  @override
  Widget build(BuildContext context) {
    final l = AppLocalizations.of(context);
    final scheme = Theme.of(context).colorScheme;
    return Scaffold(
      appBar: AppBar(title: Text(l.legalAcceptTitle)),
      body: SafeArea(
        child: Center(
          child: SingleChildScrollView(
            padding: const EdgeInsets.all(24),
            child: ConstrainedBox(
              constraints: const BoxConstraints(maxWidth: 480),
              child: ListenableBuilder(
                listenable: AuthStore.instance,
                builder: (context, _) {
                  final auth = AuthStore.instance;
                  final busy = auth.acceptingLegal;
                  return Column(
                    crossAxisAlignment: CrossAxisAlignment.stretch,
                    children: [
                      const ExcludeSemantics(child: Center(child: BrandMark())),
                      const SizedBox(height: 16),
                      Semantics(
                        header: true,
                        child: Text(
                          l.legalAcceptHeading,
                          textAlign: TextAlign.center,
                          style: Theme.of(context).textTheme.titleLarge,
                        ),
                      ),
                      const SizedBox(height: 8),
                      Text(
                        l.legalAcceptIntro,
                        textAlign: TextAlign.center,
                        style: TextStyle(color: scheme.onSurfaceVariant),
                      ),
                      const SizedBox(height: 16),
                      Wrap(
                        alignment: WrapAlignment.center,
                        spacing: 16,
                        children: [
                          ExternalLink(
                            key: const Key('legal-terms-link'),
                            text: l.legalTermsLink,
                            url: AppConfig.termsUrl,
                            hint: l.legalLinkHint,
                            failureMessage: l.legalLinkFailed,
                            opener: widget.opener,
                          ),
                          ExternalLink(
                            key: const Key('legal-privacy-link'),
                            text: l.legalPrivacyLink,
                            url: AppConfig.privacyUrl,
                            hint: l.legalLinkHint,
                            failureMessage: l.legalLinkFailed,
                            opener: widget.opener,
                          ),
                        ],
                      ),
                      const SizedBox(height: 8),
                      // The tile's title IS the checkbox's accessible name, and
                      // the whole row is the (≥48dp) tap target.
                      CheckboxListTile(
                        key: const Key('legal-accept-checkbox'),
                        value: _ticked,
                        onChanged: busy || _appOutdated
                            ? null
                            : (v) => setState(() {
                                  _ticked = v ?? false;
                                  if (_ticked) _showTickRequired = false;
                                }),
                        controlAffinity: ListTileControlAffinity.leading,
                        contentPadding: EdgeInsets.zero,
                        title: Text(l.legalAcceptCheckbox),
                        subtitle: _showTickRequired
                            ? Text(
                                l.legalAcceptTickRequired,
                                style: TextStyle(color: scheme.error),
                              )
                            : null,
                      ),
                      if (_appOutdated) ...[
                        const SizedBox(height: 8),
                        _Notice(
                          key: const Key('legal-app-outdated'),
                          text: l.legalAcceptAppOutdated,
                        ),
                      ] else if (auth.legalError != null) ...[
                        const SizedBox(height: 8),
                        _Notice(
                          key: const Key('legal-accept-error'),
                          text: auth.legalError!,
                        ),
                      ],
                      const SizedBox(height: 16),
                      if (!_appOutdated)
                        FilledButton(
                          onPressed: busy ? null : _accept,
                          style: FilledButton.styleFrom(
                            minimumSize: const Size.fromHeight(48),
                          ),
                          child: busy
                              ? const SizedBox(
                                  width: 20,
                                  height: 20,
                                  child: CircularProgressIndicator(
                                    strokeWidth: 2,
                                  ),
                                )
                              : Text(l.legalAcceptButton),
                        ),
                      const SizedBox(height: 8),
                      TextButton(
                        onPressed: busy
                            ? null
                            : () => AuthStore.instance.logout(),
                        style: TextButton.styleFrom(
                          minimumSize: const Size.fromHeight(48),
                        ),
                        child: Text(l.legalAcceptSignOut),
                      ),
                    ],
                  );
                },
              ),
            ),
          ),
        ),
      ),
    );
  }
}

/// An error notice in the theme's error container colours (AA in light and
/// dark: the scheme pairs them).
class _Notice extends StatelessWidget {
  final String text;

  const _Notice({super.key, required this.text});

  @override
  Widget build(BuildContext context) {
    final scheme = Theme.of(context).colorScheme;
    return Container(
      padding: const EdgeInsets.all(12),
      decoration: BoxDecoration(
        color: scheme.errorContainer,
        borderRadius: BorderRadius.circular(8),
      ),
      child: Semantics(
        liveRegion: true,
        child: Text(text, style: TextStyle(color: scheme.onErrorContainer)),
      ),
    );
  }
}
