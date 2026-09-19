import 'package:flutter/material.dart';

import '../models/dashboard.dart';
import '../services/api_client.dart';

/// The supervisor mobile "Dashboard" tab — mirrors the web dashboard's
/// zone coverage summary, Supervisor Queue, and active task list, with
/// the same actions (confirm/dismiss/extend/resolve/reassign/complete),
/// against /api/employee/dashboard/* (see main.py's
/// require_employee_supervisor).
class SupervisorDashboardScreen extends StatefulWidget {
  const SupervisorDashboardScreen({super.key});

  @override
  State<SupervisorDashboardScreen> createState() => _SupervisorDashboardScreenState();
}

class _SupervisorDashboardScreenState extends State<SupervisorDashboardScreen> {
  bool _loading = true;
  String? _error;
  List<ZoneState> _zones = [];
  List<QueueItem> _queue = [];
  List<DashboardTask> _tasks = [];

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
      final results = await Future.wait([
        ApiClient.instance.fetchZoneStates(),
        ApiClient.instance.fetchSupervisorQueue(),
        ApiClient.instance.fetchDashboardTasks(),
      ]);
      if (!mounted) return;
      setState(() {
        _zones = results[0] as List<ZoneState>;
        _queue = results[1] as List<QueueItem>;
        _tasks = (results[2] as List<DashboardTask>).where((t) => t.isOpen).toList();
      });
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

  Future<void> _runAction(Future<void> Function() action) async {
    try {
      await action();
      await _load();
    } on ApiException catch (e) {
      if (!mounted) return;
      ScaffoldMessenger.of(context).showSnackBar(SnackBar(content: Text(e.message)));
    } catch (e) {
      if (!mounted) return;
      ScaffoldMessenger.of(context)
          .showSnackBar(const SnackBar(content: Text('Network error — check your connection and try again.')));
    }
  }

  Future<void> _extendDialog(String taskId) async {
    final controller = TextEditingController(text: '15');
    final minutesText = await showDialog<String>(
      context: context,
      builder: (context) => AlertDialog(
        title: const Text('Extend by how many minutes?'),
        content: TextField(controller: controller, keyboardType: TextInputType.number, autofocus: true),
        actions: [
          TextButton(onPressed: () => Navigator.pop(context), child: const Text('Cancel')),
          FilledButton(onPressed: () => Navigator.pop(context, controller.text.trim()), child: const Text('Extend')),
        ],
      ),
    );
    final minutes = double.tryParse(minutesText ?? '');
    if (minutes == null || minutes <= 0) return;
    await _runAction(() => ApiClient.instance.dashboardExtendTask(taskId, minutes));
  }

  Future<void> _reassignDialog(String taskId) async {
    final controller = TextEditingController();
    final target = await showDialog<String>(
      context: context,
      builder: (context) => AlertDialog(
        title: const Text('Reassign to employee #'),
        content: TextField(controller: controller, autofocus: true),
        actions: [
          TextButton(onPressed: () => Navigator.pop(context), child: const Text('Cancel')),
          FilledButton(onPressed: () => Navigator.pop(context, controller.text.trim()), child: const Text('Reassign')),
        ],
      ),
    );
    if (target == null || target.isEmpty) return;
    await _runAction(() => ApiClient.instance.dashboardReassignTask(taskId, target));
  }

  @override
  Widget build(BuildContext context) {
    if (_loading && _tasks.isEmpty && _queue.isEmpty && _zones.isEmpty) {
      return const Center(child: CircularProgressIndicator());
    }
    return RefreshIndicator(
      onRefresh: _load,
      child: ListView(
        padding: const EdgeInsets.all(16),
        children: [
          if (_error != null) Padding(
            padding: const EdgeInsets.only(bottom: 12),
            child: Text(_error!, style: const TextStyle(color: Colors.red)),
          ),
          _sectionHeader('Zone coverage'),
          _zoneSummary(),
          const SizedBox(height: 24),
          _sectionHeader('Supervisor queue (${_queue.length})'),
          if (_queue.isEmpty) const Text('Nothing needs attention right now.'),
          ..._queue.map(_queueCard),
          const SizedBox(height: 24),
          _sectionHeader('Active tasks (${_tasks.length})'),
          if (_tasks.isEmpty) const Text('No open tasks.'),
          ..._tasks.map(_taskCard),
        ],
      ),
    );
  }

  Widget _sectionHeader(String text) => Padding(
        padding: const EdgeInsets.only(bottom: 8),
        child: Text(text, style: Theme.of(context).textTheme.titleMedium?.copyWith(fontWeight: FontWeight.bold)),
      );

  Widget _zoneSummary() {
    final covered = _zones.where((z) => z.status == 'covered').length;
    final gaps = _zones.where((z) => z.status != 'covered').length;
    return Card(
      child: Padding(
        padding: const EdgeInsets.all(12),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Text('$covered covered · $gaps needing attention'),
            const SizedBox(height: 8),
            Wrap(
              spacing: 8,
              runSpacing: 8,
              children: _zones
                  .map((z) => Chip(
                        label: Text('${z.zoneId}: ${z.status}'),
                        backgroundColor: z.status == 'covered' ? Colors.green.shade100 : Colors.orange.shade100,
                      ))
                  .toList(),
            ),
          ],
        ),
      ),
    );
  }

  Widget _queueCard(QueueItem item) {
    String subtitle;
    List<Widget> actions;
    switch (item.kind) {
      case 'extension_requested':
        subtitle = '${item.assignedTo ?? 'Unknown'} asked for more time. '
            'Active: ${item.activeMinutes.toStringAsFixed(1)} of ${item.assignedMinutes.toStringAsFixed(0)} min.';
        actions = [
          FilledButton(onPressed: () => _extendDialog(item.taskId), child: const Text('Extend')),
        ];
        break;
      case 'review_requested':
        subtitle = '${item.assignedTo ?? 'Unknown'} asked a supervisor to look at this.';
        actions = [
          FilledButton(
              onPressed: () => _runAction(() => ApiClient.instance.dashboardResolveReview(item.taskId)),
              child: const Text('Resolve — reviewed')),
        ];
        break;
      default: // flagged
        subtitle = 'Marked complete with only ${item.activeMinutes.toStringAsFixed(1)} of '
            '${item.assignedMinutes.toStringAsFixed(0)} min active (elapsed: ${item.elapsedMinutes.toStringAsFixed(1)} min).';
        actions = [
          FilledButton(
              onPressed: () => _runAction(() => ApiClient.instance.confirmFlag(item.taskId)),
              child: const Text('Confirm')),
          const SizedBox(width: 8),
          OutlinedButton(
              onPressed: () => _runAction(() => ApiClient.instance.dismissFlag(item.taskId)),
              child: const Text('Dismiss')),
        ];
    }
    return Card(
      margin: const EdgeInsets.only(bottom: 8),
      child: Padding(
        padding: const EdgeInsets.all(12),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Text(item.taskName, style: const TextStyle(fontWeight: FontWeight.bold)),
            Text(item.zoneName, style: Theme.of(context).textTheme.bodySmall),
            const SizedBox(height: 6),
            Text(subtitle),
            const SizedBox(height: 8),
            Row(children: actions),
          ],
        ),
      ),
    );
  }

  Widget _taskCard(DashboardTask task) {
    return Card(
      margin: const EdgeInsets.only(bottom: 8),
      child: Padding(
        padding: const EdgeInsets.all(12),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Text(task.taskName, style: const TextStyle(fontWeight: FontWeight.bold)),
            Text('${task.zoneName} · assigned to ${task.assignedTo ?? "nobody"}'),
            Text('Active: ${task.activeMinutes.toStringAsFixed(1)} · '
                'Elapsed: ${task.elapsedMinutes.toStringAsFixed(1)} · Budget: ${task.assignedMinutes.toStringAsFixed(0)} min'),
            const SizedBox(height: 8),
            Row(
              children: [
                FilledButton(
                    onPressed: () => _runAction(() => ApiClient.instance.dashboardCompleteTask(task.taskId)),
                    child: const Text('Mark complete')),
                const SizedBox(width: 8),
                OutlinedButton(
                    onPressed: () => _reassignDialog(task.taskId), child: const Text('Reassign')),
              ],
            ),
          ],
        ),
      ),
    );
  }
}
