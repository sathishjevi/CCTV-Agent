import 'package:flutter/material.dart';

import '../services/live_updates.dart';
import '../services/token_storage.dart';
import 'change_password_dialog.dart';
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
/// Either way a burger-menu drawer mirrors the web dashboard's top menu, gated
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
  String _name = '';

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
    final name = await TokenStorage.instance.readEmployeeName();
    if (!mounted) return;
    setState(() {
      _role = role ?? 'supervisor';
      _name = name ?? '';
    });
  }

  @override
  void dispose() {
    _tabController.dispose();
    super.dispose();
  }

  Future<void> _logout() async {
    LiveUpdates.instance.stop();
    await TokenStorage.instance.clear();
    if (!mounted) return;
    Navigator.of(context).pushAndRemoveUntil(
      MaterialPageRoute(builder: (_) => const PhoneEntryScreen()),
      (route) => false,
    );
  }

  @override
  Widget build(BuildContext context) {
    // The menu is for admin and Secondary Admin only: a phone-login
    // 'secondary_admin', or a dashboard-account 'supervisor' (which is what
    // "Secondary Admin" is called there). A plain phone-login supervisor
    // just gets the dashboard content, no menu.
    final canManage = _role == 'admin' ||
        _role == 'secondary_admin' ||
        (_role == 'supervisor' && widget.dashboardLogin);
    void open(Widget screen) {
      Navigator.of(context).pop(); // close the drawer
      Navigator.of(context).push(MaterialPageRoute(builder: (_) => screen));
    }

    return Scaffold(
      // The default leading button for a Scaffold with a drawer is the
      // burger (☰) icon.
      drawer: !canManage
          ? null
          : Drawer(
        child: SafeArea(
          child: ListView(
            children: [
              ListTile(
                title: Text(_name.isEmpty ? 'Floorwatch' : _name,
                    style: const TextStyle(fontWeight: FontWeight.bold, fontSize: 18)),
                subtitle: Text(_role == 'secondary_admin' || (_role == 'supervisor' && widget.dashboardLogin)
                    ? 'Secondary Admin'
                    : _role),
              ),
              const Divider(),
              // Same gates as the web dashboard's top menu.
              if (canManage)
                ListTile(
                  leading: const Icon(Icons.badge_outlined),
                  title: const Text('Manage Employees'),
                  onTap: () => open(const ManageEmployeesScreen()),
                ),
              if (canManage)
                ListTile(
                  leading: const Icon(Icons.map_outlined),
                  title: const Text('Manage Zones'),
                  onTap: () => open(const ManageZonesScreen()),
                ),
              if (canManage)
                ListTile(
                leading: const Icon(Icons.history),
                title: const Text('History'),
                onTap: () => open(const HistoryScreen()),
              ),
              if (_role == 'admin')
                ListTile(
                  leading: const Icon(Icons.manage_accounts_outlined),
                  title: const Text('Manage Users'),
                  onTap: () => open(const ManageUsersScreen()),
                ),
              const Divider(),
              ListTile(
                leading: const Icon(Icons.lock_reset),
                title: const Text('Change password'),
                onTap: () {
                  Navigator.of(context).pop();
                  showChangePasswordDialog(context);
                },
              ),
              ListTile(
                leading: const Icon(Icons.logout),
                title: const Text('Log out'),
                onTap: _logout,
              ),
            ],
          ),
        ),
      ),
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
          // No menu for a plain supervisor, so change-password lives here.
          if (!canManage)
            IconButton(
              icon: const Icon(Icons.lock_reset),
              tooltip: 'Change password',
              onPressed: () => showChangePasswordDialog(context),
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
