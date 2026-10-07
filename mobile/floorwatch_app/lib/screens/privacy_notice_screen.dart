import 'package:flutter/material.dart';

import '../services/api_client.dart';

/// The privacy notice (served by the backend, generated from its live
/// configuration — see privacy_notice.py). With [requireAcceptance] it is a
/// gate: the person must read it and tap "I have read and understood", which
/// records which VERSION they acknowledged; a changed notice has a new version
/// and so asks everyone again.
class PrivacyNoticeScreen extends StatefulWidget {
  final bool requireAcceptance;
  const PrivacyNoticeScreen({super.key, this.requireAcceptance = false});

  @override
  State<PrivacyNoticeScreen> createState() => _PrivacyNoticeScreenState();
}

class _PrivacyNoticeScreenState extends State<PrivacyNoticeScreen> {
  Map<String, dynamic>? _notice;
  String? _error;
  bool _accepting = false;

  @override
  void initState() {
    super.initState();
    _load();
  }

  Future<void> _load() async {
    setState(() => _error = null);
    try {
      final notice = await ApiClient.instance.fetchPrivacyNotice();
      if (mounted) setState(() => _notice = notice);
    } on ApiException catch (e) {
      if (mounted) setState(() => _error = e.message);
    } catch (_) {
      if (mounted) setState(() => _error = 'Network error — check your connection and try again.');
    }
  }

  Future<void> _accept() async {
    setState(() {
      _accepting = true;
      _error = null;
    });
    try {
      await ApiClient.instance.acceptPrivacyNotice(_notice!['version'] as String);
      if (mounted) Navigator.of(context).pop(true);
    } on ApiException catch (e) {
      // 409: the notice changed while it was open — load the new one for them to read.
      if (mounted) {
        setState(() => _error = e.message);
        if (e.statusCode == 409) _load();
      }
    } catch (_) {
      if (mounted) setState(() => _error = 'Network error — check your connection and try again.');
    } finally {
      if (mounted) setState(() => _accepting = false);
    }
  }

  @override
  Widget build(BuildContext context) {
    final notice = _notice;
    return PopScope(
      canPop: !widget.requireAcceptance,
      child: Scaffold(
        appBar: AppBar(
          title: const Text('Privacy notice'),
          automaticallyImplyLeading: !widget.requireAcceptance,
        ),
        body: SafeArea(
          child: notice == null
              ? Center(
                  child: _error == null
                      ? const CircularProgressIndicator()
                      : Column(mainAxisSize: MainAxisSize.min, children: [
                          Text(_error!, style: const TextStyle(color: Colors.red)),
                          const SizedBox(height: 12),
                          OutlinedButton(onPressed: _load, child: const Text('Try again')),
                        ]),
                )
              : Column(children: [
                  Expanded(
                    child: ListView(
                      padding: const EdgeInsets.all(20),
                      children: [
                        for (final section in (notice['sections'] as List).cast<Map<String, dynamic>>()) ...[
                          Padding(
                            padding: const EdgeInsets.only(top: 14, bottom: 4),
                            child: Text(section['heading'] as String,
                                style: Theme.of(context).textTheme.titleMedium?.copyWith(fontWeight: FontWeight.bold)),
                          ),
                          for (final paragraph in (section['paragraphs'] as List).cast<String>())
                            Padding(padding: const EdgeInsets.only(bottom: 6), child: SelectableText(paragraph)),
                        ],
                        const SizedBox(height: 12),
                        Text('Version ${notice['version']}', style: Theme.of(context).textTheme.bodySmall),
                      ],
                    ),
                  ),
                  if (widget.requireAcceptance)
                    Padding(
                      padding: const EdgeInsets.fromLTRB(20, 8, 20, 16),
                      child: Column(children: [
                        if (_error != null)
                          Padding(
                            padding: const EdgeInsets.only(bottom: 8),
                            child: Text(_error!, style: const TextStyle(color: Colors.red)),
                          ),
                        SizedBox(
                          width: double.infinity,
                          child: FilledButton(
                            onPressed: _accepting ? null : _accept,
                            child: _accepting
                                ? const SizedBox(height: 18, width: 18, child: CircularProgressIndicator(strokeWidth: 2))
                                : const Text('I have read and understood'),
                          ),
                        ),
                      ]),
                    ),
                ]),
        ),
      ),
    );
  }
}

/// Wrap a signed-in employee's home screen in this: once, shortly after it
/// first appears, it asks the backend whether this person has acknowledged the
/// CURRENT notice and, if not, shows it full-screen until they do.
///
/// Fails OPEN on a network error — this is a duty to inform, not a security
/// barrier, so being offline mustn't lock someone out of their tasks; it simply
/// asks again next time. If the organisation hasn't finished configuring the
/// notice (no name/contact yet) it doesn't ask either: nobody should be made to
/// acknowledge a notice with placeholders in it.
class PrivacyGate extends StatefulWidget {
  final Widget child;
  const PrivacyGate({super.key, required this.child});

  @override
  State<PrivacyGate> createState() => _PrivacyGateState();
}

class _PrivacyGateState extends State<PrivacyGate> {
  @override
  void initState() {
    super.initState();
    WidgetsBinding.instance.addPostFrameCallback((_) => _check());
  }

  Future<void> _check() async {
    try {
      final status = await ApiClient.instance.fetchPrivacyStatus();
      if (!mounted || status['accepted'] == true || status['notice_configured'] != true) return;
      await Navigator.of(context).push(MaterialPageRoute(
        fullscreenDialog: true,
        builder: (_) => const PrivacyNoticeScreen(requireAcceptance: true),
      ));
    } catch (_) {
      // fail open — see the class comment
    }
  }

  @override
  Widget build(BuildContext context) => widget.child;
}
