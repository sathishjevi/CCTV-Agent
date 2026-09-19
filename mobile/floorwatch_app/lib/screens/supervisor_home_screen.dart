import 'package:flutter/material.dart';

import '../services/token_storage.dart';
import 'history_screen.dart';
import 'manage_employees_screen.dart';
import 'manage_users_screen.dart';
import 'manage_zones_screen.dart';
import 'phone_entry_screen.dart';
import 'supervisor_dashboard_screen.dart';
import 'task_list_screen.dart';

/// Landing screen for a supervisor/admin login. Two ways in:
///  - phone+password employee login (role supervisor/admin): a top tab bar
///    with "My Tasks" (their own assigned tasks) and "Dashboard", default
///    Dashboard;
///  - dashboard username+password login ([dashboardLogin]): no employee
///    identity, so just the Dashboard (no "My Tasks" tab).
/// Either way an overflow menu mirrors the web dashboard's top menu, gated
/// by role the same way the web UI does: Manage Employees/Zones for
/// admin+supervisor, History for everyone, Manage Users for admin only.
class SupervisorHomeScreen extends StatefulWidget {
  final bool dashboardLogin;
  const SupervisorHomeScreen({super.key, this.dashboardLogin = false});

  @override
  State<SupervisorHomeScreen> createState() => _SupervisorHomeScreenState();
}

class _SupervisorHomeScreenState extends State<SupervisorHomeScreen> with SingleTickerProviderStateMixin {
  late final TabController _tabController;
  String _role = 'supervisor';

  @override
  void initState() {
    super.initState();
    // Order is [My Tasks, Dashboard] but a supervisor's default landing
    // tab is Dashboard (index 1) — they're checking on the floor first,
    // not their own task list.
    _tabController = TabController(length: 2, vsync: this, initialIndex: 1);
    _loadRole();
  }

  Future<void> _loadRole() async {
    final role = await TokenStorage.instance.readRole();
    if (!mounted) return;
    setState(() => _role = role ?? 'supervisor');
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
    final canManage = _role == 'admin' || _role == 'supervisor';
    return Scaffold(
      appBar: AppBar(
        title: const Text('Floorwatch'),
        bottom: widget.dashboardLogin
            ? null
            : TabBar(
                controller: _tabController,
                tabs: const [
                  Tab(icon: Icon(Icons.checklist), text: 'My Tasks'),
                  Tab(icon: Icon(Icons.dashboard), text: 'Dashboard'),
                ],
              ),
        actions: [
          PopupMenuButton<String>(
            icon: const Icon(Icons.more_vert),
            onSelected: (value) {
              switch (value) {
                case 'employees':
                  Navigator.of(context).push(MaterialPageRoute(builder: (_) => const ManageEmployeesScreen()));
                  break;
                case 'zones':
                  Navigator.of(context).push(MaterialPageRoute(builder: (_) => const ManageZonesScreen()));
                  break;
                case 'history':
                  Navigator.of(context).push(MaterialPageRoute(builder: (_) => const HistoryScreen()));
                  break;
                case 'users':
                  Navigator.of(context).push(MaterialPageRoute(builder: (_) => const ManageUsersScreen()));
                  break;
              }
            },
            itemBuilder: (context) => [
              // Same gates as the web dashboard's top menu.
              if (canManage) const PopupMenuItem(value: 'employees', child: Text('Manage Employees')),
              if (canManage) const PopupMenuItem(value: 'zones', child: Text('Manage Zones')),
              const PopupMenuItem(value: 'history', child: Text('History')),
              if (_role == 'admin') const PopupMenuItem(value: 'users', child: Text('Manage Users')),
            ],
          ),
          IconButton(icon: const Icon(Icons.logout), onPressed: _logout),
        ],
      ),
      body: widget.dashboardLogin
          ? const SupervisorDashboardScreen()
          : TabBarView(
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
