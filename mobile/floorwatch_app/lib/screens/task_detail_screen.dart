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

  Future<void> _run(Future<void> Function() action, String successMessage) async {
    setState(() {
      _busy = true;
      _error = null;
      _info = null;
    });
    try {
      await action();
      if (!mounted) return;
      setState(() => _info = successMessage);
    } on ApiException catch (e) {
      if (!mounted) return;
      setState(() => _error = e.message);
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
            Text('Status: ${task.statusLabel}'),
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
            if (!_busy) ...[
              Wrap(
                spacing: 12,
                runSpacing: 12,
                children: [
                  FilledButton(
                    onPressed: () => _run(() => ApiClient.instance.startTask(task.taskId), 'Marked in progress.'),
                    child: const Text('Start'),
                  ),
                  FilledButton(
                    onPressed: () => _run(() => ApiClient.instance.completeTask(task.taskId), 'Marked complete.'),
                    child: const Text('Done'),
                  ),
                  OutlinedButton(
                    onPressed: () => _run(
                      () => ApiClient.instance.requestExtension(task.taskId),
                      'Extension requested — a supervisor will follow up.',
                    ),
                    child: const Text('Need more time'),
                  ),
                  OutlinedButton(
                    onPressed: () => _run(
                      () => ApiClient.instance.requestReview(task.taskId),
                      'Review requested — a supervisor will check in.',
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
