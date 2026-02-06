import * as vscode from 'vscode';
import { FlaCli } from './cli';

/**
 * Tree data provider for the Fla History panel.
 */
export class FlaHistoryProvider implements vscode.TreeDataProvider<HistoryItem> {
  private cli: FlaCli;
  private _onDidChangeTreeData = new vscode.EventEmitter<HistoryItem | undefined>();
  readonly onDidChangeTreeData = this._onDidChangeTreeData.event;

  constructor(cli: FlaCli) {
    this.cli = cli;
  }

  refresh(): void {
    this._onDidChangeTreeData.fire(undefined);
  }

  getTreeItem(element: HistoryItem): vscode.TreeItem {
    return element;
  }

  async getChildren(): Promise<HistoryItem[]> {
    try {
      const history = await this.cli.getHistory(undefined, 30);
      return history.map((t: any) => new HistoryItem(t));
    } catch {
      return [];
    }
  }
}

class HistoryItem extends vscode.TreeItem {
  constructor(transition: any) {
    const id = (transition.id || '').substring(0, 8);
    const prompt = (transition.prompt || '(no message)').substring(0, 50);
    super(`${id} ${prompt}`, vscode.TreeItemCollapsibleState.None);

    this.description = transition.lane || '';
    this.tooltip = `${transition.status}\n${transition.prompt}\nLane: ${transition.lane}`;

    // Icon based on status
    const status = transition.status || 'proposed';
    if (status === 'accepted') {
      this.iconPath = new vscode.ThemeIcon('check', new vscode.ThemeColor('testing.iconPassed'));
    } else if (status === 'rejected') {
      this.iconPath = new vscode.ThemeIcon('x', new vscode.ThemeColor('testing.iconFailed'));
    } else {
      this.iconPath = new vscode.ThemeIcon('circle-outline');
    }
  }
}
