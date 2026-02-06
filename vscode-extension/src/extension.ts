import * as vscode from 'vscode';
import { FlaCli } from './cli';
import { FlaStatusBar } from './statusbar';
import { FlaHistoryProvider } from './history';

let statusBar: FlaStatusBar | undefined;
let refreshInterval: ReturnType<typeof setInterval> | undefined;

export function activate(context: vscode.ExtensionContext) {
  const cli = new FlaCli();

  // Status bar
  statusBar = new FlaStatusBar(cli);
  context.subscriptions.push(statusBar);

  // Commands
  context.subscriptions.push(
    vscode.commands.registerCommand('fla.status', () => cli.showStatus()),
    vscode.commands.registerCommand('fla.snapshot', () => cli.snapshot()),
    vscode.commands.registerCommand('fla.commit', () => cli.commit()),
    vscode.commands.registerCommand('fla.history', () => cli.showHistory()),
    vscode.commands.registerCommand('fla.lanes', () => cli.showLanes()),
    vscode.commands.registerCommand('fla.createLane', () => cli.createLane()),
    vscode.commands.registerCommand('fla.switchLane', () => cli.switchLane()),
    vscode.commands.registerCommand('fla.diff', () => cli.showDiff()),
  );

  // History tree view
  const historyProvider = new FlaHistoryProvider(cli);
  context.subscriptions.push(
    vscode.window.registerTreeDataProvider('flaHistory', historyProvider),
  );

  // Auto-refresh
  const config = vscode.workspace.getConfiguration('fla');
  if (config.get<boolean>('autoRefresh', true)) {
    const interval = config.get<number>('refreshInterval', 5000);
    refreshInterval = setInterval(() => statusBar?.refresh(), interval);
  }

  // Initial refresh
  statusBar.refresh();
}

export function deactivate() {
  if (refreshInterval) clearInterval(refreshInterval);
  statusBar?.dispose();
}
