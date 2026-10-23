import { render } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { describe, expect, it } from 'vitest';
import {
  Alert, Badge, Button, Card, Checkbox, Dialog, DialogFooter, DialogHeader, Dropdown,
  IconButton, Input, Kbd, KbdShortcut, PageContainer, PageHeader, Select, Switch, Table,
  TableShell, TabPanel, Tabs, Textarea, Tooltip,
} from '../components/ui';
import { axe } from '../test/axe';

describe('ui primitives have no axe violations', () => {
  it('buttons, badges, cards, alerts and keyboard hints', async () => {
    const { container } = render(
      <main>
        <Button>Save</Button>
        <Button variant="danger" loading>Deleting</Button>
        <IconButton label="Close panel">x</IconButton>
        <Badge label="pro" />
        <Card><p>Card body</p></Card>
        <Alert variant="danger" title="Failed">Something failed</Alert>
        <KbdShortcut keys={['Cmd', 'K']} />
        <Kbd>Esc</Kbd>
      </main>,
    );
    expect(await axe(container)).toHaveNoViolations();
  });

  it('labelled form controls', async () => {
    const { container } = render(
      <form>
        <Input title="Name" />
        <Textarea title="Notes" />
        <Select title="Tier" value="a" onValueChange={() => {}} options={[{ value: 'a', label: 'A' }]} />
        <Select aria-label="Sort" value="a" onValueChange={() => {}} options={[{ value: 'a', label: 'A' }]} />
        <Checkbox label="Remember me" />
        <Switch checked={false} onChange={() => {}} label="Auto sync" />
      </form>,
    );
    expect(await axe(container)).toHaveNoViolations();
  });

  it('tabs and table', async () => {
    const { container } = render(
      <main>
        <Tabs
          aria-label="Sections"
          idPrefix="t"
          activeKey="a"
          onChange={() => {}}
          bare
          tabs={[{ key: 'a', label: 'Alpha' }, { key: 'b', label: 'Beta' }]}
        />
        <TabPanel idPrefix="t" tabKey="a"><p>Panel</p></TabPanel>
        <Table
          columns={[{ key: 'n', header: 'Name', render: (r: { n: string }) => r.n }]}
          data={[{ n: 'Ada' }]}
          rowKey={(r) => r.n}
        />
        <TableShell>
          <thead><tr><th scope="col">Name</th></tr></thead>
          <tbody><tr><td>Ada</td></tr></tbody>
        </TableShell>
      </main>,
    );
    expect(await axe(container)).toHaveNoViolations();
  });

  it('dialog, dropdown, tooltip', async () => {
    const { baseElement } = render(
      <main>
        <Dialog open onClose={() => {}} titleId="d-title">
          <DialogHeader titleId="d-title">Confirm</DialogHeader>
          <p>Are you sure?</p>
          <DialogFooter><Button>OK</Button></DialogFooter>
        </Dialog>
        <Dropdown
          trigger={<button type="button" aria-haspopup="menu">Menu</button>}
          items={[{ key: 'one', label: 'One' }]}
          onSelect={() => {}}
          menuLabel="Actions"
        />
        <Tooltip content="Hint"><button type="button">Hover</button></Tooltip>
      </main>,
    );
    expect(await axe(baseElement)).toHaveNoViolations();
  });

  it('page shell: PageContainer + PageHeader', async () => {
    const { container } = render(
      <MemoryRouter>
        <main>
          <PageContainer>
            <PageHeader
              title="Projects"
              description="All your projects"
              breadcrumb={<a href="/">Home</a>}
              actions={<Button>New</Button>}
            />
          </PageContainer>
        </main>
      </MemoryRouter>,
    );
    expect(await axe(container)).toHaveNoViolations();
  });

  it('actually reports violations (guards against a silently disabled matcher)', async () => {
    const { container } = render(<button type="button" />);
    const results = await axe(container);
    expect(results.violations.map((v) => v.id)).toContain('button-name');
  });
});
