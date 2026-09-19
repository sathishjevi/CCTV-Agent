import 'dart:async';
import 'dart:convert';
import 'dart:io';

import 'api_client.dart';
import 'token_storage.dart';

/// Live-update channel to the backend's /ws/app socket.
///
/// The socket carries no data — only {"hint": "refresh", ...} when something
/// this login is allowed to care about changed (a plain employee: their own
/// tasks; a supervisor/admin/dashboard login: anything on the floor). Screens
/// listen to [hints] and refetch over the normal authenticated REST calls, so
/// nothing sensitive ever travels over the socket itself.
///
/// Reconnects on its own with a capped backoff, and stops when [stop] is
/// called (logout) or the stored token disappears.
class LiveUpdates {
  LiveUpdates._();
  static final LiveUpdates instance = LiveUpdates._();

  final _controller = StreamController<Map<String, dynamic>>.broadcast();
  Stream<Map<String, dynamic>> get hints => _controller.stream;

  WebSocket? _socket;
  Timer? _retryTimer;
  bool _running = false;
  int _attempt = 0;

  /// Idempotent — every screen that wants live updates can just call this.
  void start() {
    if (_running) return;
    _running = true;
    _attempt = 0;
    _connect();
  }

  void stop() {
    _running = false;
    _retryTimer?.cancel();
    _retryTimer = null;
    _socket?.close();
    _socket = null;
  }

  Future<void> _connect() async {
    if (!_running) return;
    try {
      final token = await TokenStorage.instance.readToken();
      if (token == null) {
        stop();
        return;
      }
      const base = ApiClient.baseUrl;
      if (base.isEmpty) return;
      final uri = Uri.parse(base);
      final wsUri = uri.replace(
        scheme: uri.scheme == 'https' ? 'wss' : 'ws',
        path: '/ws/app',
        queryParameters: {'token': token},
      );
      final socket = await WebSocket.connect(wsUri.toString()).timeout(const Duration(seconds: 15));
      // Keeps proxies from dropping an idle connection.
      socket.pingInterval = const Duration(seconds: 25);
      _socket = socket;
      _attempt = 0;
      socket.listen(
        (data) {
          try {
            final decoded = jsonDecode(data as String);
            if (decoded is Map<String, dynamic>) _controller.add(decoded);
          } catch (_) {}
        },
        onDone: _scheduleReconnect,
        onError: (_) => _scheduleReconnect(),
        cancelOnError: true,
      );
    } catch (_) {
      _scheduleReconnect();
    }
  }

  void _scheduleReconnect() {
    _socket = null;
    if (!_running) return;
    _retryTimer?.cancel();
    final seconds = (2 << (_attempt < 4 ? _attempt : 4)).clamp(2, 30);
    _attempt++;
    _retryTimer = Timer(Duration(seconds: seconds), _connect);
  }
}

/// Coalesces a burst of hints (one action often emits several events) into a
/// single refresh.
class Debouncer {
  final Duration delay;
  Timer? _timer;
  Debouncer(this.delay);

  void run(void Function() action) {
    _timer?.cancel();
    _timer = Timer(delay, action);
  }

  void dispose() => _timer?.cancel();
}
