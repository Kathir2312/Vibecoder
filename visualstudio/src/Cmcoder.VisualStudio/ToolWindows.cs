using Microsoft.VisualStudio.Shell;
using System;
using System.Linq;
using System.Runtime.InteropServices;
using System.Windows;
using System.Windows.Controls;

namespace Cmcoder.VisualStudio
{
    /// <summary>
    /// The chat (H5): the shared chat page. Above it, a strip with code search's state (H16) and
    /// buttons for the Agent Navigator and the Tasks tab: a tool window has no title-bar buttons
    /// of its own (VS Code shows the same two there), and Tools > cmcoder is easy to miss.
    /// </summary>
    [Guid("e2a9c4f7-1b6d-4e3a-8c5f-9d2b7a1e4c36")]
    public sealed class ChatWindow : ToolWindowPane
    {
        private readonly Panel panel;
        private readonly Button codeSearch = StripButton();
        private readonly WrapPanel strip = new WrapPanel();
        private IDisposable? stopCodeSearch;

        /// <summary>A button of the strip, in Visual Studio's own style (it follows the theme).</summary>
        private static Button StripButton(string? text = null, string? tip = null, Action? click = null)
        {
            var button = new Button { HorizontalAlignment = HorizontalAlignment.Left, Padding = new Thickness(6, 1, 6, 1), Margin = new Thickness(2) };
            button.SetResourceReference(Control.StyleProperty, Microsoft.VisualStudio.Shell.VsResourceKeys.ButtonStyleKey);
            if (text != null) button.Content = text;
            if (tip != null) button.ToolTip = tip;
            if (click != null) button.Click += (_, __) => click();
            return button;
        }

        public ChatWindow() : base(null)
        {
            Caption = Brand.Product;
            var session = Session.Get();
            panel = new Panel("chat", json => session.Run(h => h.OnPanelMessage(json)));
            codeSearch.Click += (_, __) => CodeSearchUi.Menu(session.CodeSearch);
            strip.Children.Add(codeSearch);
            strip.Children.Add(StripButton("Agent Navigator", "Open the Agent Navigator: this turn's agents as a mind map, in a tab",
                () => CmcoderPackage.Instance?.ShowNavigator()));
            strip.Children.Add(StripButton("Tasks", "Open the agent's whole task list in a tab",
                () => CmcoderPackage.Instance?.ShowTasks()));
            panel.AddTop(strip);
            Content = panel;
            ShowCodeSearch();
        }

        protected override void Initialize()
        {
            base.Initialize();
            var session = Session.Get();
            // Registered before the page loads: its "ready" may come at once.
            session.ChatOpened(panel);
            stopCodeSearch = session.OnCodeSearchChange(() => Ui.Later(async () =>
            {
                await ThreadHelper.JoinableTaskFactory.SwitchToMainThreadAsync();
                ShowCodeSearch();
            }));
            Ui.Later(() => panel.LoadAsync());
        }

        private void ShowCodeSearch()
        {
            var search = Session.Get().CodeSearch;
            var text = search.Text;
            codeSearch.Content = text ?? "Code search";
            codeSearch.ToolTip = text == null ? "Code search (when " + Brand.Product + " is running)" : search.Tooltip;
        }

        internal Panel Panel => panel;

        /// <summary>For tests: the code search item's text.</summary>
        internal string CodeSearchText => codeSearch.Content as string ?? "";

        /// <summary>For tests: what the strip's buttons say, in order.</summary>
        internal string[] StripTexts => strip.Children.OfType<Button>().Select(b => b.Content as string ?? "").ToArray();

        /// <summary>For tests: a click on the strip's button with this text; false if there is none.</summary>
        internal bool ClickStrip(string text)
        {
            var button = strip.Children.OfType<Button>().FirstOrDefault(b => (b.Content as string) == text);
            if (button == null) return false;
            button.RaiseEvent(new RoutedEventArgs(System.Windows.Controls.Primitives.ButtonBase.ClickEvent));
            return true;
        }

        protected override void Dispose(bool disposing)
        {
            if (disposing)
            {
                stopCodeSearch?.Dispose();
                Session.Get().ChatClosed(panel);
                panel.Close();
            }
            base.Dispose(disposing);
        }
    }

    /// <summary>The Tasks tab, a document tab: the agent's whole todo list (the chat shows one compact line).</summary>
    [Guid("bc8cb086-689a-455e-abdb-1d2ae651a535")]
    public sealed class TasksWindow : ToolWindowPane
    {
        private readonly Panel panel;

        public TasksWindow() : base(null)
        {
            Caption = Brand.Product + ": Tasks";
            var session = Session.Get();
            panel = new Panel("tasks", json => session.Run(h => h.OnTasksMessage(json)));
            Content = panel;
        }

        protected override void Initialize()
        {
            base.Initialize();
            Session.Get().TasksOpened(panel);
            Ui.Later(() => panel.LoadAsync());
        }

        internal Panel Panel => panel;

        protected override void Dispose(bool disposing)
        {
            if (disposing)
            {
                Session.Get().TasksClosed(panel);
                panel.Close();
            }
            base.Dispose(disposing);
        }
    }

    /// <summary>The Agent Navigator (H15), a document tab: the current turn's agents.</summary>
    [Guid("5c8e1a3b-7d4f-4b2e-9a6c-3e7f1d9b5a28")]
    public sealed class NavigatorWindow : ToolWindowPane
    {
        private readonly Panel panel;

        public NavigatorWindow() : base(null)
        {
            Caption = Brand.Product + ": Agent Navigator";
            var session = Session.Get();
            panel = new Panel("navigator", json => session.Run(h => h.OnNavigatorMessage(json)));
            Content = panel;
        }

        protected override void Initialize()
        {
            base.Initialize();
            Session.Get().NavigatorOpened(panel);
            Ui.Later(() => panel.LoadAsync());
        }

        internal Panel Panel => panel;

        protected override void Dispose(bool disposing)
        {
            if (disposing)
            {
                Session.Get().NavigatorClosed(panel);
                panel.Close();
            }
            base.Dispose(disposing);
        }
    }
}
