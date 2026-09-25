import 'dart:async';

import 'package:flutter/material.dart';

import '../models/task.dart';
import '../services/api_client.dart';
import '../services/live_updates.dart';
import '../services/token_storage.dart';
import 'change_password_dialog.dart';
import 'phone_entry_screen.dart';
import 'task_detail_screen.dart';

class TaskListScreen extends StatefulWidget {
  // When embedded inside SupervisorHomeScreen's "My Tasks" tab, the
  // parent already provides a Scaffold/AppBar/logout button — showing
  // this screen's own would double them up.
  final bool embedded;
  const TaskListScreen({super.key, this.embedded = false});

  @override
  State<TaskListScreen> createState() => _TaskListScreenState();
}

class _TaskListScreenState extends State<TaskListScreen> with WidgetsBindingObserver {
  List<EmployeeTask> _tasks = [];
  bool _loading = true;
  String? _error;

  StreamSubscription<Map<String, dynamic>>? _hintSub;
  final _debounce = Debouncer(const Duration(milliseconds: 800));

  @override
  void initState() {
    super.initState();
    WidgetsBinding.instance.addObserver(this);
    _load();
    // A hint means one of THIS employee's tasks changed (assigned, extended,
    // reassigned away, ...) — refetch quietly instead of waiting for a pull.
    LiveUpdates.instance.start();
    _hintSub = LiveUpdates.instance.hints.listen((hint) {
      if (hint['event_type'] == 'task_active_time_update') return;
      _debounce.run(() {
        if (mounted) _load(silent: true);
      });
    });
  }

  @override
  void dispose() {
    WidgetsBinding.instance.removeObserver(this);
    _hintSub?.cancel();
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
      final tasks = await ApiClient.instance.fetchTasks();
      if (!mounted) return;
      setState(() {
        _tasks = tasks;
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

  Future<void> _logout() async {
    LiveUpdates.instance.stop();
    await ApiClient.instance.logout(); // needs the token, so before it's cleared
    await TokenStorage.instance.clear();
    if (!mounted) return;
    Navigator.of(context).pushAndRemoveUntil(
      MaterialPageRoute(builder: (_) => const PhoneEntryScreen()),
      (route) => false,
    );
  }

  @override
  Widget build(BuildContext context) {
    final body = RefreshIndicator(onRefresh: _load, child: _buildBody());
    if (widget.embedded) return body;
    return Scaffold(
      appBar: AppBar(
        title: const Text('My tasks'),
        actions: [
          IconButton(
            icon: const Icon(Icons.lock_reset),
            tooltip: 'Change password',
            onPressed: () => showChangePasswordDialog(context),
          ),
          IconButton(icon: const Icon(Icons.logout), onPressed: _logout),
        ],
      ),
      body: body,
    );
  }

  Widget _buildBody() {
    if (_loading && _tasks.isEmpty) {
      return const Center(child: CircularProgressIndicator());
    }
    if (_error != null) {
      return ListView(
        children: [
          Padding(
            padding: const EdgeInsets.all(24),
            child: Text(_error!, style: const TextStyle(color: Colors.red)),
          ),
        ],
      );
    }
    if (_tasks.isEmpty) {
      return ListView(
        children: const [
          Padding(
            padding: EdgeInsets.all(32),
            child: Center(child: Text('No open tasks right now. Pull down to refresh.')),
          ),
        ],
      );
    }
    return ListView.separated(
      itemCount: _tasks.length,
      separatorBuilder: (_, __) => const Divider(height: 1),
      itemBuilder: (context, index) {
        final task = _tasks[index];
        return ListTile(
          title: Text(task.taskName),
          subtitle: Text('${task.zoneName} · ${task.statusLabel}'),
          trailing: Text('${task.assignedMinutes.toStringAsFixed(0)}m'),
          onTap: () async {
            await Navigator.of(context).push(
              MaterialPageRoute(builder: (_) => TaskDetailScreen(task: task)),
            );
            _load(); // refresh in case an action changed the task's status
          },
        );
      },
    );
  }
}
