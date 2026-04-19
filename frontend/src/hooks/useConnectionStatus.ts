import { useEffect, useState } from 'react';
import { eventBus } from '../events/EventBus';

export function useConnectionStatus(): boolean {
  const [connected, setConnected] = useState(eventBus.connected);
  useEffect(() => eventBus.onStatus(setConnected), []);
  return connected;
}
