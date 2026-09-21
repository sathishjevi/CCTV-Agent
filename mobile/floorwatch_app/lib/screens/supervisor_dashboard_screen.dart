import 'dart:async';

import 'package:flutter/material.dart';

import '../models/admin.dart';
import '../models/dashboard.dart';
import '../services/api_client.dart';
import '../services/live_updates.dart';
import '../services/token_storage.dart';

/// The supervisor mobile "Dashboard" — the web dashboard's sections, in
/// the same order and with the same wording: stat counters, Floor Status
/// (coverage), Supervisor Queue (coverage-gap directives + flagged /
/// extension / review tasks), Assigned Tasks (with the assign form), and
/// the event feed. Works against /api/employee/dashboard/* for a phone
/// login and the regular /api/* endpoints for an admin (username) login —
/// ApiClient maps between them.
///
/// Refreshes live: the backend's /ws/app socket sends a "something changed"
/// hint (no data) and this re-reads everything over REST; a slow poll and
/// a refresh on returning to the app cover a dropped connection.
class SupervisorDashboardScreen extends StatefulWidget {
  const SupervisorDashboardScreen({super.key});

  @override
  State<SupervisorDashboardScreen> createState() => _SupervisorDashboardScreenState();
}

class _SupervisorDashboardScreenState extends State<SupervisorDashboardScreen> with WidgetsBindingObserver {
  // Same list as the web dashboard's Type selector.
  static const _taskTypes = {
    'clean_door': 'clean_door',
    'restock_concession': 'restock_concession',
    'restroom_check': 'restroom_check',
    'lobby_sweep': 'lobby_sweep',
    '_default': 'other',
  };

  bool _loading = true;
  String? _error;
  bool _canAssign = false;
  List<ZoneState> _zoneStates = [];
  List<ZoneRecord> _zoneDirectory = [];
  List<ZoneDirective> _directives = [];
  List<QueueItem> _queue = [];
  List<DashboardTask> _tasks = [];
  List<EmployeeRecord> _employees = [];
  List<HistoryEvent> _history = [];
  StreamSubscription<Map<String, dynamic>>? _hintSub;
  Timer? _fallbackTimer;
  final _debounce = Debouncer(const Duration(seconds: 1));

  Map<String, String> get _employeeNames => {for (final e in _employees) e.employeeNumber: e.name};

  @override
  void initState() {
    super.initState();
    WidgetsBinding.instance.addObserver(this);
    TokenStorage.instance.readRole().then((role) {
      if (mounted) setState(() => _canAssign = role == 'admin' || role == 'secondary_admin' || role == 'supervisor');
    });
    _load();
    LiveUpdates.instance.start();
    _hintSub = LiveUpdates.instance.hints.listen((hint) {
      // Active-time ticks arrive constantly; the slow poll covers those.
      if (hint['event_type'] == 'task_active_time_update') return;
      _debounce.run(() {
        if (mounted) _load(silent: true);
      });
    });
    _fallbackTimer = Timer.periodic(const Duration(seconds: 60), (_) {
      if (mounted && !_loading) _load(silent: true);
    });
  }

  @override
  void dispose() {
    WidgetsBinding.instance.removeObserver(this);
    _hintSub?.cancel();
    _fallbackTimer?.cancel();
    _debounce.dispose();
    super.dispose();
  }

  @override
  void didChangeAppLifecycleState(AppLifecycleState state) {
    if (state == AppLifecycleState.resumed) {
      LiveUpdates.instance.start();
      _load(silent: true);
    }
  }

  Future<void> _load({bool silent = false}) async {
    if (!silent) {
      setState(() {
        _loading = true;
        _error = null;
      });
    }
    try {
      final results = await Future.wait([
        ApiClient.instance.fetchZoneStates(),
        ApiClient.instance.fetchSupervisorQueue(),
        ApiClient.instance.fetchDashboardTasks(),
        ApiClient.instance.fetchZones(),
        ApiClient.instance.fetchEmployees().catchError((_) => <EmployeeRecord>[]),
        ApiClient.instance.fetchHistory(limit: 500),
        ApiClient.instance.fetchZoneDirectives().catchError((_) => <ZoneDirective>[]),
      ]);
      if (!mounted) return;
      setState(() {
        _zoneStates = results[0] as List<ZoneState>;
        _queue = results[1] as List<QueueItem>;
        _tasks = (results[2] as List<DashboardTask>).where((t) => t.isOpen).toList();
        _zoneDirectory = (results[3] as List<ZoneRecord>).where((z) => z.active).toList();
        _employees = results[4] as List<EmployeeRecord>;
        _history = results[5] as List<HistoryEvent>;
        _directives = results[6] as List<ZoneDirective>;
        _error = null;
      });
    } on ApiException catch (e) {
      if (!mounted || silent) return;
      setState(() => _error = e.message);
    } catch (e) {
      if (!mounted || silent) return;
      setState(() => _error = 'Network error — pull down to retry.');
    } finally {
      if (mounted && !silent) setState(() => _loading = false);
    }
  }

  Future<void> _runAction(Future<void> Function() action) async {
    try {
      await action();
      await _load(silent: true);
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

  Future<void> _reassignDialog(String taskId, String? currentAssignee) async {
    final active = _employees.where((e) => e.active).toList();
    String? selected = active.any((e) => e.employeeNumber == currentAssignee) ? currentAssignee : null;
    final controller = TextEditingController();
    final target = await showDialog<String>(
      context: context,
      builder: (context) => StatefulBuilder(
        builder: (context, setDialogState) => AlertDialog(
          title: const Text('Reassign'),
          content: active.isEmpty
              // A login that can't list employees falls back to typing a number.
              ? TextField(
                  controller: controller,
                  autofocus: true,
                  decoration: const InputDecoration(labelText: 'Employee number'),
                )
              : DropdownButtonFormField<String>(
                  initialValue: selected,
                  isExpanded: true,
                  hint: const Text('Choose an employee'),
                  items: active
                      .map((e) => DropdownMenuItem(
                            value: e.employeeNumber,
                            child: Text('${e.name} — ${e.department} (#${e.employeeNumber})',
                                overflow: TextOverflow.ellipsis),
                          ))
                      .toList(),
                  onChanged: (v) => setDialogState(() => selected = v),
                ),
          actions: [
            TextButton(onPressed: () => Navigator.pop(context), child: const Text('Cancel')),
            FilledButton(
              onPressed: () => Navigator.pop(context, active.isEmpty ? controller.text.trim() : selected),
              child: const Text('Reassign'),
            ),
          ],
        ),
      ),
    );
    if (target == null || target.isEmpty) return;
    await _runAction(() => ApiClient.instance.dashboardReassignTask(taskId, target));
  }

  Future<void> _assignTaskDialog() async {
    final nameController = TextEditingController();
    final minutesController = TextEditingController(text: '60');
    final activeEmployees = _employees.where((e) => e.active).toList();
    String? zoneId = _zoneDirectory.isNotEmpty ? _zoneDirectory.first.zoneId : null;
    String taskType = _taskTypes.keys.first;
    String? assignee;
    String? formError;

    final ok = await showDialog<bool>(
      context: context,
      builder: (context) => StatefulBuilder(
        builder: (context, setDialogState) => AlertDialog(
          title: const Text('Assign task'),
          content: SingleChildScrollView(
            child: Column(
              mainAxisSize: MainAxisSize.min,
              children: [
                TextField(
                  controller: nameController,
                  decoration: const InputDecoration(labelText: 'Task', hintText: 'e.g. Clean Door — Zone 4'),
                ),
                DropdownButtonFormField<String>(
                  initialValue: zoneId,
                  isExpanded: true,
                  decoration: const InputDecoration(labelText: 'Zone'),
                  items: _zoneDirectory
                      .map((z) => DropdownMenuItem(value: z.zoneId, child: Text(z.name, overflow: TextOverflow.ellipsis)))
                      .toList(),
                  onChanged: (v) => setDialogState(() => zoneId = v),
                ),
                DropdownButtonFormField<String>(
                  initialValue: taskType,
                  isExpanded: true,
                  decoration: const InputDecoration(labelText: 'Type'),
                  items: _taskTypes.entries
                      .map((t) => DropdownMenuItem(value: t.key, child: Text(t.value)))
                      .toList(),
                  onChanged: (v) => setDialogState(() => taskType = v ?? taskType),
                ),
                TextField(
                  controller: minutesController,
                  keyboardType: TextInputType.number,
                  decoration: const InputDecoration(labelText: 'Budget (min)'),
                ),
                DropdownButtonFormField<String?>(
                  initialValue: assignee,
                  isExpanded: true,
                  decoration: const InputDecoration(labelText: 'Assign to'),
                  items: [
                    const DropdownMenuItem<String?>(value: null, child: Text('Unassigned')),
                    ...activeEmployees.map((e) => DropdownMenuItem<String?>(
                          value: e.employeeNumber,
                          child: Text('${e.name} — ${e.department} (#${e.employeeNumber})',
                              overflow: TextOverflow.ellipsis),
                        )),
                  ],
                  onChanged: (v) => setDialogState(() => assignee = v),
                ),
                if (formError != null)
                  Padding(
                    padding: const EdgeInsets.only(top: 8),
                    child: Text(formError!, style: const TextStyle(color: Colors.red)),
                  ),
              ],
            ),
          ),
          actions: [
            TextButton(onPressed: () => Navigator.pop(context, false), child: const Text('Cancel')),
            FilledButton(
              onPressed: () {
                final minutes = double.tryParse(minutesController.text.trim());
                if (nameController.text.trim().isEmpty || minutes == null || minutes <= 0 || zoneId == null) {
                  setDialogState(() => formError = 'Task name, a zone and a positive minute budget are required.');
                  return;
                }
                Navigator.pop(context, true);
              },
              child: const Text('Assign task'),
            ),
          ],
        ),
      ),
    );
    if (ok != true || zoneId == null) return;
    final chosenZone = zoneId!;
    await _runAction(() => ApiClient.instance.assignTask(
          taskName: nameController.text.trim(),
          zoneId: chosenZone,
          assignedMinutes: double.parse(minutesController.text.trim()),
          taskType: taskType,
          assignedTo: assignee,
        ));
  }

  // ── build ─────────────────────────────────────────────────────────────

  @override
  Widget build(BuildContext context) {
    if (_loading && _tasks.isEmpty && _queue.isEmpty && _zoneDirectory.isEmpty) {
      return const Center(child: CircularProgressIndicator());
    }
    final pending = _directives.length + _queue.length;
    return RefreshIndicator(
      onRefresh: _load,
      child: ListView(
        padding: const EdgeInsets.all(16),
        children: [
          if (_error != null)
            Padding(
              padding: const EdgeInsets.only(bottom: 12),
              child: Text(_error!, style: const TextStyle(color: Colors.red)),
            ),
          _statsStrip(),
          const SizedBox(height: 12),
          _zoneCoverage(),
          const SizedBox(height: 24),
          _sectionHeader('SUPERVISOR QUEUE', '$pending pending'),
          if (pending == 0) const Text('No pending items.'),
          ..._directives.map(_directiveCard),
          ..._queue.map(_queueCard),
          const SizedBox(height: 24),
          _sectionHeader('ASSIGNED TASKS — EFFORT TRACKING', '${_tasks.length} active'),
          if (_canAssign)
            Padding(
              padding: const EdgeInsets.only(bottom: 8),
              child: Align(
                alignment: Alignment.centerLeft,
                child: FilledButton.icon(
                  onPressed: _assignTaskDialog,
                  icon: const Icon(Icons.add),
                  label: const Text('Assign task'),
                ),
              ),
            ),
          if (_tasks.isEmpty) const Text('No tasks assigned yet.'),
          ..._tasks.map(_taskCard),
          const SizedBox(height: 24),
          _eventFeed(),
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

  // ── stat counters ─────────────────────────────────────────────────────

  Widget _statsStrip() {
    final covered = _zoneDirectory.where((z) => _statusFor(z.zoneId) == 'covered').length;
    final stats = DashboardStats.from(zonesCovered: covered, newestFirst: _history);
    Widget tile(String label, int value, Color color, {String? sub}) => Expanded(
          child: Card(
            margin: const EdgeInsets.symmetric(horizontal: 3),
            child: Padding(
              padding: const EdgeInsets.symmetric(vertical: 10, horizontal: 6),
              child: Column(
                children: [
                  Text('$value', style: TextStyle(fontSize: 22, fontWeight: FontWeight.bold, color: color)),
                  Text(label, textAlign: TextAlign.center, style: const TextStyle(fontSize: 10.5)),
                  if (sub != null) Text(sub, style: TextStyle(fontSize: 10, color: Colors.grey.shade600)),
                ],
              ),
            ),
          ),
        );
    return Column(
      children: [
        Row(children: [
          tile('Zones covered', stats.zonesCovered, Colors.green),
          tile('Coverage nudges', stats.nudges, Colors.orange.shade800),
        ]),
        const SizedBox(height: 6),
        Row(children: [
          tile('Effort flags raised', stats.effortFlags, Colors.purple, sub: '${stats.effortFlagsResolved} resolved'),
          tile('Supervisor actions', stats.supervisorActions, Colors.red),
        ]),
      ],
    );
  }

  // ── zone coverage ─────────────────────────────────────────────────────

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

  // ── event feed ────────────────────────────────────────────────────────

  String _clock(String? iso) {
    final t = iso == null ? null : DateTime.tryParse(iso)?.toLocal();
    if (t == null) return '';
    String two(int n) => n.toString().padLeft(2, '0');
    return '${two(t.hour)}:${two(t.minute)}:${two(t.second)}';
  }

  Widget _eventFeed() {
    final feed = _history.take(40).toList();
    return Card(
      child: ExpansionTile(
        initiallyExpanded: false,
        shape: const Border(),
        collapsedShape: const Border(),
        title: const Text('EVENT FEED',
            style: TextStyle(fontWeight: FontWeight.bold, fontSize: 13, letterSpacing: 0.5)),
        subtitle: Text('${feed.length} events'),
        childrenPadding: const EdgeInsets.fromLTRB(12, 0, 12, 12),
        children: [
          if (feed.isEmpty) const Padding(padding: EdgeInsets.all(8), child: Text('No events yet.')),
          ...feed.map((e) => Padding(
                padding: const EdgeInsets.symmetric(vertical: 4),
                child: Row(
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: [
                    Text(_clock(e.timestamp), style: Theme.of(context).textTheme.bodySmall),
                    const SizedBox(width: 8),
                    Expanded(
                      child: Text(e.message ??
                          '${e.eventType}${e.actionType != null ? " (${e.actionType})" : ""}'),
                    ),
                  ],
                ),
              )),
        ],
      ),
    );
  }

  // ── supervisor queue cards ────────────────────────────────────────────

  Widget _directiveCard(ZoneDirective d) {
    return Card(
      margin: const EdgeInsets.only(bottom: 8),
      child: Padding(
        padding: const EdgeInsets.all(12),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Row(children: [
              Expanded(child: Text(d.zoneName, style: const TextStyle(fontWeight: FontWeight.bold))),
              Text('coverage gap unresolved', style: Theme.of(context).textTheme.bodySmall),
            ]),
            const SizedBox(height: 6),
            const Text('Auto-drafted directive ready for your approval:'),
            Container(
              margin: const EdgeInsets.only(top: 4),
              padding: const EdgeInsets.all(8),
              decoration: BoxDecoration(
                color: Colors.grey.withValues(alpha: 0.12),
                borderRadius: BorderRadius.circular(6),
              ),
              child: Text('"${d.message}"'),
            ),
            const SizedBox(height: 8),
            Wrap(spacing: 8, runSpacing: 8, children: [
              FilledButton(
                  onPressed: () => _runAction(() => ApiClient.instance.approveZoneDirective(d.zoneId)),
                  child: const Text('Approve & send')),
              OutlinedButton(
                  onPressed: () => _runAction(() => ApiClient.instance.reassignZoneCoverage(d.zoneId)),
                  child: const Text('Reassign coverage')),
            ]),
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
        subtitle = '${_employeeLabel(item.assignedTo)} asked for more time. '
            'Active: ${item.activeMinutes.toStringAsFixed(1)} of ${item.assignedMinutes.toStringAsFixed(0)} min.';
        actions = [
          FilledButton(onPressed: () => _extendDialog(item.taskId), child: const Text('Extend')),
        ];
        break;
      case 'review_requested':
        subtitle = '${_employeeLabel(item.assignedTo)} asked a supervisor to look at this.';
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

  // ── task cards ────────────────────────────────────────────────────────

  // Same wording and colours as the web dashboard's WORKFLOW_LABELS.
  static const _workflowLabels = {
    'unassigned': ('Unassigned', Colors.blueGrey),
    'notified': ('Notified — waiting to start', Colors.blueGrey),
    'notify_failed': ('Notification failed — assignee not reached', Colors.red),
    'in_progress': ('In progress', Colors.green),
    'awaiting_update': ('Pending — waiting for update from employee', Colors.orange),
    'extension_requested': ('Employee asked for more time — see queue', Colors.orange),
    'review_requested': ('Employee asked for review — see queue', Colors.orange),
    'completed': ('Completed', Colors.green),
  };
  static const _needsAttention = {'notify_failed', 'awaiting_update', 'extension_requested', 'review_requested'};

  Widget _taskCard(DashboardTask task) {
    // Same pace logic as the web card's bar: green unless active time is
    // well behind elapsed time.
    final assigned = task.assignedMinutes <= 0 ? 1.0 : task.assignedMinutes;
    final pct = (task.activeMinutes / assigned * 100).round().clamp(0, 100);
    final expectedPct = (task.elapsedMinutes / assigned * 100).round().clamp(0, 100);
    Color barColor = Colors.green;
    if (pct < expectedPct - 30) {
      barColor = Colors.red;
    } else if (pct < expectedPct - 10) {
      barColor = Colors.orange;
    }
    final workflow = _workflowLabels[task.workflowStatus];
    // "On track" is only about active-time pace — next to a workflow badge
    // that already signals a problem it reads as a contradiction, so it's
    // dropped there (same rule as the web card).
    final showOnTrack = !_needsAttention.contains(task.workflowStatus);

    return Card(
      margin: const EdgeInsets.only(bottom: 8),
      child: Padding(
        padding: const EdgeInsets.all(12),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Row(children: [
              Expanded(child: Text(task.taskName, style: const TextStyle(fontWeight: FontWeight.bold))),
              Text(task.zoneName, style: Theme.of(context).textTheme.bodySmall),
            ]),
            const SizedBox(height: 8),
            LinearProgressIndicator(
              value: pct / 100,
              minHeight: 6,
              color: barColor,
              backgroundColor: Colors.grey.withValues(alpha: 0.2),
              borderRadius: BorderRadius.circular(3),
            ),
            const SizedBox(height: 6),
            Row(
              mainAxisAlignment: MainAxisAlignment.spaceBetween,
              children: [
                Text('Active: ${task.activeMinutes.toStringAsFixed(1)} / ${task.assignedMinutes.toStringAsFixed(0)} min',
                    style: Theme.of(context).textTheme.bodySmall),
                Text('Elapsed: ${task.elapsedMinutes.toStringAsFixed(1)} / ${task.assignedMinutes.toStringAsFixed(0)} min',
                    style: Theme.of(context).textTheme.bodySmall),
              ],
            ),
            if (showOnTrack)
              const Padding(
                padding: EdgeInsets.only(top: 4),
                child: Text('On track', style: TextStyle(color: Colors.green, fontSize: 12)),
              ),
            Padding(
              padding: const EdgeInsets.only(top: 4),
              child: Text('Assigned to: ${_employeeLabel(task.assignedTo)}',
                  style: Theme.of(context).textTheme.bodySmall),
            ),
            if (workflow != null)
              Padding(
                padding: const EdgeInsets.only(top: 6),
                child: Chip(
                  visualDensity: VisualDensity.compact,
                  label: Text(workflow.$1, style: TextStyle(fontSize: 11, color: workflow.$2.shade800)),
                  backgroundColor: workflow.$2.withValues(alpha: 0.12),
                  side: BorderSide(color: workflow.$2.withValues(alpha: 0.5)),
                ),
              ),
            const SizedBox(height: 8),
            Wrap(
              spacing: 8,
              runSpacing: 8,
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
                OutlinedButton(
                    onPressed: () => _reassignDialog(task.taskId, task.assignedTo),
                    child: Text(task.assignedTo == null ? 'Assign' : 'Reassign')),
              ],
            ),
          ],
        ),
      ),
    );
  }
}
