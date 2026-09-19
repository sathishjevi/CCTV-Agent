import 'package:flutter/material.dart';

import '../models/admin.dart';
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
  List<ZoneState> _zoneStates = [];
  List<ZoneRecord> _zoneDirectory = [];
  List<QueueItem> _queue = [];
  List<DashboardTask> _tasks = [];
  Map<String, String> _employeeNames = {};

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
        ApiClient.instance.fetchZones(),
        ApiClient.instance.fetchEmployees(),
      ]);
      if (!mounted) return;
      setState(() {
        _zoneStates = results[0] as List<ZoneState>;
        _zoneDirectory = (results[3] as List<ZoneRecord>).where((z) => z.active).toList();
        _employeeNames = {for (final e in results[4] as List<EmployeeRecord>) e.employeeNumber: e.name};
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
    if (_loading && _tasks.isEmpty && _queue.isEmpty && _zoneDirectory.isEmpty) {
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
          _zoneCoverage(),
          const SizedBox(height: 24),
          _sectionHeader('SUPERVISOR QUEUE', '${_queue.length} pending'),
          if (_queue.isEmpty) const Text('No pending items.'),
          ..._queue.map(_queueCard),
          const SizedBox(height: 24),
          _sectionHeader('ASSIGNED TASKS — EFFORT TRACKING', '${_tasks.length} active'),
          if (_tasks.isEmpty) const Text('No tasks assigned yet.'),
          ..._tasks.map(_taskCard),
        ],
      ),
    );
  }

  Widget _sectionHeader(String text, String count) => Padding(
        padding: const EdgeInsets.only(bottom: 8),
        child: Row(
          children: [
            Expanded(
              child: Text(text,
                  style: const TextStyle(fontWeight: FontWeight.bold, fontSize: 13, letterSpacing: 0.5)),
            ),
            Text(count, style: Theme.of(context).textTheme.bodySmall),
          ],
        ),
      );

  String _employeeLabel(String? number) {
    if (number == null) return 'nobody';
    final name = _employeeNames[number];
    return name == null ? '#$number' : '$name (#$number)';
  }

  static const _statusOrder = ['covered', 'gap', 'nudge', 'command', 'escalated'];
  static const _statusLabels = {
    'covered': 'Covered',
    'gap': 'Gap detected',
    'nudge': 'Nudge sent',
    'command': 'Command issued',
    'escalated': 'Escalated',
  };

  // Same source as the web dashboard's Floor Status panel: the active zone
  // directory, each defaulting to "covered" until a live state says otherwise
  // (/api/state only lists zones the engine has seen an event for).
  String _statusFor(String zoneId) {
    for (final z in _zoneStates) {
      if (z.zoneId == zoneId) return z.status;
    }
    return 'covered';
  }

  String _coverageSummary() {
    if (_zoneDirectory.isEmpty) return 'No zones configured';
    final counts = <String, int>{};
    for (final z in _zoneDirectory) {
      final s = _statusFor(z.zoneId);
      counts[s] = (counts[s] ?? 0) + 1;
    }
    final keys = [..._statusOrder.where(counts.containsKey), ...counts.keys.where((k) => !_statusOrder.contains(k))];
    return keys.map((k) => '${counts[k]} ${(_statusLabels[k] ?? k).toLowerCase()}').join(', ');
  }

  Color _statusColor(String status) {
    switch (status) {
      case 'covered':
        return Colors.green;
      case 'escalated':
        return Colors.red;
      default:
        return Colors.orange;
    }
  }

  Widget _zoneCoverage() {
    return Card(
      child: ExpansionTile(
        initiallyExpanded: false,
        shape: const Border(),
        collapsedShape: const Border(),
        title: const Text('FLOOR STATUS — COVERAGE',
            style: TextStyle(fontWeight: FontWeight.bold, fontSize: 13, letterSpacing: 0.5)),
        subtitle: Text('${_zoneDirectory.length} zones · ${_coverageSummary()}'),
        childrenPadding: const EdgeInsets.fromLTRB(12, 0, 12, 12),
        children: [
          if (_zoneDirectory.isEmpty) const Padding(padding: EdgeInsets.all(8), child: Text('No zones configured yet.')),
          LayoutBuilder(builder: (context, constraints) {
            final width = (constraints.maxWidth - 8) / 2;
            return Wrap(
              spacing: 8,
              runSpacing: 8,
              children: _zoneDirectory.map((z) {
                final status = _statusFor(z.zoneId);
                final color = _statusColor(status);
                return Container(
                  width: width,
                  padding: const EdgeInsets.all(10),
                  decoration: BoxDecoration(
                    border: Border.all(color: color.withValues(alpha: 0.5)),
                    borderRadius: BorderRadius.circular(8),
                    color: color.withValues(alpha: 0.06),
                  ),
                  child: Column(
                    crossAxisAlignment: CrossAxisAlignment.start,
                    children: [
                      Text(z.name, style: const TextStyle(fontWeight: FontWeight.bold)),
                      Text(z.roleTag.toUpperCase(), style: Theme.of(context).textTheme.labelSmall),
                      const SizedBox(height: 8),
                      Row(children: [
                        Icon(Icons.circle, size: 8, color: color),
                        const SizedBox(width: 6),
                        Text(_statusLabels[status] ?? status, style: TextStyle(color: color, fontSize: 12)),
                      ]),
                    ],
                  ),
                );
              }).toList(),
            );
          }),
        ],
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
              child: const Text('Follow up')),
          const SizedBox(width: 8),
          OutlinedButton(
              onPressed: () => _runAction(() => ApiClient.instance.dismissFlag(item.taskId)),
              child: const Text('Looks Fine')),
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
            Text('${task.zoneName} · assigned to ${_employeeLabel(task.assignedTo)}'),
            Text('Active: ${task.activeMinutes.toStringAsFixed(1)} · '
                'Elapsed: ${task.elapsedMinutes.toStringAsFixed(1)} · Budget: ${task.assignedMinutes.toStringAsFixed(0)} min'),
            const SizedBox(height: 8),
            Row(
              children: [
                // Same rule as the web card: a task reopened via "Follow up with
                // Employee" is closed with "Reviewed — looks good" (resolve-review),
                // not Mark complete, which would just re-run the flag check.
                if (task.reopenedForReview)
                  FilledButton(
                      onPressed: () => _runAction(() => ApiClient.instance.dashboardResolveReview(task.taskId)),
                      child: const Text('Reviewed — looks good'))
                else
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
