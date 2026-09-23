import 'package:flutter/material.dart';

import '../models/task.dart';
import '../services/api_client.dart';

class TaskDetailScreen extends StatefulWidget {
  final EmployeeTask task;
  const TaskDetailScreen({super.key, required this.task});

  @override
  State<TaskDetailScreen> createState() => _TaskDetailScreenState();
}

class _TaskDetailScreenState extends State<TaskDetailScreen> {
  bool _busy = false;
  String? _error;
  String? _info;

  // widget.task is a static snapshot handed down by the list screen —
  // it never changes after a successful action, which is why Start kept
  // showing even after the task had already moved to "in progress."
  // This tracks the REAL current status locally, updated the moment an
  // action actually succeeds, so the button row and the status line
  // both reflect it immediately without needing to back out and refetch.
  late String _workflowStatus = widget.task.workflowStatus;
  // Flips true as soon as this screen opens (see initState) — cosmetic
  // only, see EmployeeTask.notificationSeen's docstring.
  late bool _notificationSeen = widget.task.notificationSeen;

  bool get _canStart =>
      _workflowStatus == 'notified' || _workflowStatus == 'notify_failed' || _workflowStatus == 'awaiting_update';
  bool get _isActionable => _workflowStatus != 'completed';

  @override
  void initState() {
    super.initState();
    // Opening the task IS the assignee checking it — flip the "seen" flag
    // so the list/dashboard stop saying "Notification sent" for this task.
    // Fire-and-forget: purely cosmetic, never worth surfacing an error for.
    if (_workflowStatus == 'notified' && !_notificationSeen) {
      ApiClient.instance.markTaskSeen(widget.task.taskId).then((_) {
        if (mounted) setState(() => _notificationSeen = true);
      }).catchError((_) {});
    }
  }

  String get _statusLabel {
    switch (_workflowStatus) {
      case 'unassigned':
        return 'Unassigned';
      case 'notified':
        return _notificationSeen ? 'Waiting for you to start' : 'Notification sent — not yet seen';
      case 'notify_failed':
        return 'Notification failed';
      case 'in_progress':
        return 'In progress';
      case 'awaiting_update':
        return 'Status update needed';
      case 'extension_requested':
        return 'Extension requested';
      case 'review_requested':
        return 'Review requested';
      case 'completed':
        return 'Completed';
      default:
        return _workflowStatus;
    }
  }

  Future<void> _run(Future<void> Function() action, String successMessage, {String? newStatus}) async {
    setState(() {
      _busy = true;
      _error = null;
      _info = null;
    });
    try {
      await action();
      if (!mounted) return;
      setState(() {
        _info = successMessage;
        if (newStatus != null) _workflowStatus = newStatus;
      });
    } on ApiException catch (e) {
      if (!mounted) return;
      setState(() => _error = e.message);
    } catch (e) {
      // Network-level failures (e.g. a reset connection) throw
      // ClientException/SocketException, not ApiException — those must
      // still reach the UI instead of vanishing silently (this was a
      // real bug: a dropped connection made action buttons look like
      // they did nothing at all).
      if (!mounted) return;
      setState(() => _error = 'Network error — check your connection and try again.');
    } finally {
      if (mounted) setState(() => _busy = false);
    }
  }

  Future<void> _reassignDialog() async {
    final controller = TextEditingController();
    final target = await showDialog<String>(
      context: context,
      builder: (context) => AlertDialog(
        title: const Text('Hand off this task'),
        content: TextField(
          controller: controller,
          decoration: const InputDecoration(labelText: 'Employee number'),
        ),
        actions: [
          TextButton(onPressed: () => Navigator.pop(context), child: const Text('Cancel')),
          FilledButton(onPressed: () => Navigator.pop(context, controller.text.trim()), child: const Text('Hand off')),
        ],
      ),
    );
    if (target == null || target.isEmpty) return;
    await _run(
      () => ApiClient.instance.reassignTask(widget.task.taskId, target),
      'Handed off to employee $target.',
    );
    // Once handed off, this employee no longer owns the task — every
    // other action button would just 403 from here on, so there's
    // nothing left to do on this screen. Back out to the list (which
    // re-fetches and will no longer include this task) instead of
    // leaving them stranded on a task that isn't theirs anymore.
    if (_error == null && mounted) Navigator.of(context).pop();
  }

  @override
  Widget build(BuildContext context) {
    final task = widget.task;
    return Scaffold(
      appBar: AppBar(title: Text(task.taskName)),
      body: Padding(
        padding: const EdgeInsets.all(24),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Text(task.zoneName, style: Theme.of(context).textTheme.titleMedium),
            const SizedBox(height: 4),
            Text('Status: $_statusLabel'),
            const SizedBox(height: 4),
            Text('Budget: ${task.assignedMinutes.toStringAsFixed(0)} min '
                '· Active: ${task.activeMinutes.toStringAsFixed(0)} min '
                '· Elapsed: ${task.elapsedMinutes.toStringAsFixed(0)} min'),
            const SizedBox(height: 4),
            Text('Task code: ${task.shortCode}'),
            const SizedBox(height: 24),
            if (_error != null) Text(_error!, style: const TextStyle(color: Colors.red)),
            if (_info != null) Text(_info!, style: const TextStyle(color: Colors.green)),
            const SizedBox(height: 8),
            if (_busy) const Center(child: CircularProgressIndicator()),
            if (!_busy && !_isActionable) const Text('This task is already completed — nothing left to do.'),
            if (!_busy && _isActionable) ...[
              Wrap(
                spacing: 12,
                runSpacing: 12,
                children: [
                  if (_canStart)
                    FilledButton(
                      onPressed: () => _run(
                        () => ApiClient.instance.startTask(task.taskId),
                        'Marked in progress.',
                        newStatus: 'in_progress',
                      ),
                      child: const Text('Start'),
                    ),
                  FilledButton(
                    onPressed: () => _run(
                      () => ApiClient.instance.completeTask(task.taskId),
                      'Marked complete.',
                      newStatus: 'completed',
                    ),
                    child: const Text('Done'),
                  ),
                  OutlinedButton(
                    onPressed: () => _run(
                      () => ApiClient.instance.requestExtension(task.taskId),
                      'Extension requested — a supervisor will follow up.',
                      newStatus: 'extension_requested',
                    ),
                    child: const Text('Need more time'),
                  ),
                  OutlinedButton(
                    onPressed: () => _run(
                      () => ApiClient.instance.requestReview(task.taskId),
                      'Review requested — a supervisor will check in.',
                      newStatus: 'review_requested',
                    ),
                    child: const Text('Ask supervisor'),
                  ),
                  OutlinedButton(onPressed: _reassignDialog, child: const Text('Hand off to someone else')),
                ],
              ),
            ],
          ],
        ),
      ),
    );
  }
}
