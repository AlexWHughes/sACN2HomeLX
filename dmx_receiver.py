import sacn
import threading
import time
import queue
from typing import Callable, Dict, List, Optional

# Merge queued packet events into stats at most this often (bounded queue latency for UI)
_STAT_DRAIN_INTERVAL_S = 0.05


class DMXReceiver:
    """E1.31 (sACN) DMX receiver"""
    
    def __init__(self, bind_ip: Optional[str] = None):
        self.bind_ip = bind_ip
        self.receiver = None
        self.universe_callbacks: Dict[int, Callable] = {}
        self.running = False
        self._stopped = False
        self._last_start_errors: List[Dict] = []
        self._lifecycle_lock = threading.RLock()
        self.stats = {
            'packets_received': 0,
            'last_packet_time': None,
            'active_universes': set(),
            'packets_per_universe': {}
        }
        self.stats_lock = threading.Lock()
        # Hot path: lock-free enqueue per packet; drain merges under stats_lock (periodic thread + get_stats)
        self._stat_queue: queue.SimpleQueue = queue.SimpleQueue()
        self._stat_drain_stop = threading.Event()
        self._stat_drain_thread: Optional[threading.Thread] = None
        self._start_receiver()
        self._start_stat_drain_thread()
    
    def _start_stat_drain_thread(self) -> None:
        if self._stat_drain_thread is not None and self._stat_drain_thread.is_alive():
            return
        self._stat_drain_stop.clear()
        self._stat_drain_thread = threading.Thread(
            target=self._stat_drain_loop,
            name='dmx_stat_drain',
            daemon=True,
        )
        self._stat_drain_thread.start()
    
    def _stat_drain_loop(self) -> None:
        while not self._stat_drain_stop.wait(timeout=_STAT_DRAIN_INTERVAL_S):
            with self.stats_lock:
                self._drain_stat_queue_locked()
    
    def _drain_stat_queue_locked(self) -> None:
        """Drain all pending (universe, time) events into self.stats; caller must hold stats_lock."""
        drained = 0
        last_t = None
        while True:
            try:
                u, t = self._stat_queue.get_nowait()
            except queue.Empty:
                break
            drained += 1
            last_t = t
            self.stats['packets_per_universe'][u] = self.stats['packets_per_universe'].get(u, 0) + 1
            self.stats['active_universes'].add(u)
        if drained:
            self.stats['packets_received'] += drained
            self.stats['last_packet_time'] = last_t
    
    def _stop_stat_drain_thread(self) -> None:
        self._stat_drain_stop.set()
        t = self._stat_drain_thread
        self._stat_drain_thread = None
        if t is not None and t.is_alive():
            t.join(timeout=0.5)
    
    def _start_receiver(self):
        """Start the sACN receiver"""
        if self.receiver is not None:
            try:
                self.receiver.stop()
            except Exception:
                pass
        
        self.receiver = sacn.sACNreceiver(self.bind_ip) if self.bind_ip else sacn.sACNreceiver()
        self.receiver.start()
        
    def listen_to_universe(self, universe: int, callback: Callable):
        """Register a callback for a specific DMX universe"""
        if not self.receiver:
            raise RuntimeError("Receiver not initialized")
        
        self.universe_callbacks[universe] = callback
        print(f"Registering listener for universe {universe}")
        
        # Create a handler function for this universe
        def handle_dmx(packet):
            try:
                if not self.running:
                    return  # Don't process if not running
                
                # Statistics: enqueue only (no lock on hot path)
                self._stat_queue.put((universe, time.time()))
                
                # Extract DMX data from packet
                dmx_data = None
                if hasattr(packet, 'dmxData'):
                    dmx_data = packet.dmxData
                elif hasattr(packet, 'dmx_data'):
                    dmx_data = packet.dmx_data
                elif hasattr(packet, 'dmx'):
                    dmx_data = packet.dmx
                elif isinstance(packet, (list, tuple)):
                    dmx_data = list(packet)
                elif hasattr(packet, '__iter__') and not isinstance(packet, (str, bytes)):
                    dmx_data = list(packet)
                else:
                    print(f"Warning: Could not extract DMX data from packet for universe {universe}. Packet type: {type(packet)}, attributes: {dir(packet)[:10]}")
                    return
                
                # Call the callback with DMX data
                if dmx_data is not None:
                    callback(dmx_data, universe)
            except Exception as e:
                print(f"Error in handle_dmx for universe {universe}: {e}")
                import traceback
                traceback.print_exc()
        
        # Register the listener using register_listener method (more reliable than decorator pattern)
        try:
            self.receiver.register_listener('universe', handle_dmx, universe=universe)
        except TypeError as e:
            print(f"Error registering listener for universe {universe}: {e}")
            raise
        
        # Join multicast for this universe
        try:
            self.receiver.join_multicast(universe)
        except Exception as e:
            print(f"Error joining multicast for universe {universe}: {e}")
            # Continue anyway as unicast might still work
    
    def _stats_snapshot_locked(self) -> Dict:
        """Build a stats dict; caller must hold stats_lock (and should drain first)."""
        self._drain_stat_queue_locked()
        receiving = False
        if self.stats['last_packet_time']:
            receiving = (time.time() - self.stats['last_packet_time']) < 2.0 and self.running
        return {
            'packets_received': self.stats['packets_received'],
            'last_packet_time': self.stats['last_packet_time'],
            'active_universes': sorted(list(self.stats['active_universes'])),
            'packets_per_universe': dict(self.stats['packets_per_universe']),
            'running': self.running,
            'receiving': receiving,
            'last_start_errors': list(self._last_start_errors),
        }

    def get_stats(self) -> Dict:
        """Get current reception statistics (drains pending packet events first)."""
        with self.stats_lock:
            return self._stats_snapshot_locked()
    
    def get_stats_nonblocking(self) -> Optional[Dict]:
        """Same as get_stats if the stats lock can be taken immediately; otherwise None."""
        if not self.stats_lock.acquire(blocking=False):
            return None
        try:
            return self._stats_snapshot_locked()
        finally:
            self.stats_lock.release()
    
    def reset_stats(self):
        """Reset statistics and clear any queued packet events."""
        with self.stats_lock:
            try:
                while True:
                    self._stat_queue.get_nowait()
            except queue.Empty:
                pass
            self.stats = {
                'packets_received': 0,
                'last_packet_time': None,
                'active_universes': set(),
                'packets_per_universe': {}
            }
    
    def start(self):
        """Start receiving DMX data"""
        with self._lifecycle_lock:
            if self._stopped:
                self._last_start_errors = []
                self._start_receiver()
                for universe, callback in list(self.universe_callbacks.items()):
                    try:
                        self.listen_to_universe(universe, callback)
                    except TypeError as e:
                        print(f"Error re-registering listener for universe {universe}: {e}")
                        self._last_start_errors.append({
                            'universe': universe,
                            'error': str(e),
                        })
                if self._last_start_errors:
                    failed = [err['universe'] for err in self._last_start_errors]
                    print(
                        f"Degraded sACN startup: failed to re-register universes {failed}; "
                        f"other universes remain listening"
                    )
            self._stopped = False
            self.running = True
            self._start_stat_drain_thread()
    
    def stop(self):
        """Stop receiving DMX data"""
        with self._lifecycle_lock:
            if self._stopped:
                return
            self._stopped = True
            self.running = False
            if self.receiver is not None:
                try:
                    self.receiver.stop()
                except Exception:
                    pass
            self._stop_stat_drain_thread()
    
    def close(self):
        """Close the receiver"""
        self.stop()
