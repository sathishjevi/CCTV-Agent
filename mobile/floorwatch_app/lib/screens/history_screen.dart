import 'package:flutter/material.dart';

import '../models/admin.dart';
import '../services/api_client.dart';

/// Mirrors the web dashboard's "History" panel — the durable audit
/// trail (/api/employee/dashboard/history, require_employee_supervisor).
class HistoryScreen extends StatefulWidget {
  const HistoryScreen({super.key});

  @override
  State<HistoryScreen> createState() => _HistoryScreenState();
}

class _HistoryScreenState extends State<HistoryScreen> {
  List<HistoryEvent> _events = [];
  bool _loading = true;
  String? _error;

  @override
  void initState() {
    super.initState();
    _load();
  }

  Future<void> _load() async {
    setState(() {
      _loading = true;
      _error = null;
    });
    try {
      final events = await ApiClient.instance.fetchHistory();
      if (!mounted) return;
      setState(() => _events = events);
    } on ApiException catch (e) {
      if (!mounted) return;
      setState(() => _error = e.message);
    } catch (e) {
      if (!mounted) return;
      setState(() => _error = 'Network error — pull down to retry.');
    } finally {
      if (mounted) setState(() => _loading = false);
    }
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      appBar: AppBar(title: const Text('History')),
      body: RefreshIndicator(
        onRefresh: _load,
        child: _loading && _events.isEmpty
            ? const Center(child: CircularProgressIndicator())
            : ListView.separated(
                padding: const EdgeInsets.all(16),
                itemCount: _error != null ? 1 : _events.length,
                separatorBuilder: (_, __) => const Divider(height: 1),
                itemBuilder: (context, index) {
                  if (_error != null) {
                    return Text(_error!, style: const TextStyle(color: Colors.red));
                  }
                  final e = _events[index];
                  final subject = e.taskName ?? e.zoneName ?? '';
                  return ListTile(
                    title: Text(e.message ?? '${e.eventType}${e.actionType != null ? " (${e.actionType})" : ""}'),
                    subtitle: Text('$subject${subject.isNotEmpty ? " · " : ""}${e.timestamp ?? ""}'),
                  );
                },
              ),
      ),
    );
  }
}
