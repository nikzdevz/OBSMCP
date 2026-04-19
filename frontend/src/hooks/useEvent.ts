import { useEffect } from 'react';
import { eventBus, OBSMCPEvent } from '../events/EventBus';

export function useEvent(handler: (event: OBSMCPEvent) => void): void {
  useEffect(() => eventBus.subscribe(handler), [handler]);
}
