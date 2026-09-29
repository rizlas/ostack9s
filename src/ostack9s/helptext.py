"""English help text (kept apart so the catalogs can import it)."""

GLOBAL_HELP = """\
Command bar (:)                          Tab accepts the suggestion, Esc closes
  :servers :volumes :nets :sg …          open a resource (aliases below)
  :region <name>   :reg                  switch region
  :project <name>  :proj                 switch project
  :cloud <name>    :ctx                  switch cloud (clouds.yaml entry)
  :overview        :ov                   every cloud × project × region
  :topology        :topo                 network topology (Mermaid/DOT export)
  :search <text>   :find                 name, ID or IP in every project and region
  :unused  :audit                        unused resources, exposed security groups
  :lang <en|it>                          interface language
  :q                                     quit

Navigation
  m                        resource menu (one key to jump)
  ↑ ↓ PgUp PgDn Home End   move the cursor
  Enter                    open the child resource or the YAML details
  Esc                      clear the filter / go back
  /                        filter rows
  d                        describe pane next to the table (Tab moves focus)
  y                        full screen YAML of the row (c copies it)
  a                        menu with every action of the resource
  Ctrl+O, header click     sort by column
  [  ]                     previous / next region
  Ctrl+R                   reload
  F1 F2 F4 F5              overview / cloud / project / region (shortcuts)
  F3                       show or hide the quota panel
"""
