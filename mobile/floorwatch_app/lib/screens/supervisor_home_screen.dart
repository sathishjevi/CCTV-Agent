import 'package:flutter/material.dart';

import '../services/token_storage.dart';
import 'phone_entry_screen.dart';
import 'supervisor_dashboard_screen.dart';
import 'task_list_screen.dart';

/// Landing screen for a supervisor login: a top tab bar with "My Tasks"
/// (their own self-assigned tasks — the same screen a plain employee
/// gets) and "Dashboard" (the supervisor mobile dashboard). A plain
/// employee never reaches this screen at all — see phone_entry_screen.dart's
/// role-based routing after login.
class SupervisorHomeScreen extends StatefulWidget {
  const SupervisorHomeScreen({super.key});

  @override
  State<SupervisorHomeScreen> createState() => _SupervisorHomeScreenState();
}

class _SupervisorHomeScreenState extends State<SupervisorHomeScreen> with SingleTickerProviderStateMixin {
  late final TabController _tabController;

  @override
  void initState() {
    super.initState();
    // Order is [My Tasks, Dashboard] but a supervisor's default landing
    // tab is Dashboard (index 1) — they're checking on the floor first,
    // not their own task list.
    _tabController = TabController(length: 2, vsync: this, initialIndex: 1);
  }

  @override
  void dispose() {
    _tabController.dispose();
    super.dispose();
  }

  Future<void> _logout() async {
    await TokenStorage.instance.clear();
    if (!mounted) return;
    Navigator.of(context).pushAndRemoveUntil(
      MaterialPageRoute(builder: (_) => const PhoneEntryScreen()),
      (route) => false,
    );
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      appBar: AppBar(
        title: const Text('Floorwatch'),
        bottom: TabBar(
          controller: _tabController,
          tabs: const [
            Tab(icon: Icon(Icons.checklist), text: 'My Tasks'),
            Tab(icon: Icon(Icons.dashboard), text: 'Dashboard'),
          ],
        ),
        actions: [IconButton(icon: const Icon(Icons.logout), onPressed: _logout)],
      ),
      body: TabBarView(
        controller: _tabController,
        children: const [
          _EmbeddedTaskList(),
          SupervisorDashboardScreen(),
        ],
      ),
    );
  }
}

/// TaskListScreen has its own Scaffold/AppBar (used standalone for a
/// plain employee) — embedded here inside a tab, only its body makes
/// sense, so this strips the Scaffold chrome rather than forking a
/// second task-list implementation.
class _EmbeddedTaskList extends StatelessWidget {
  const _EmbeddedTaskList();

  @override
  Widget build(BuildContext context) {
    return const TaskListScreen(embedded: true);
  }
}
