# Changelog

All notable changes to ostack9s are listed here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/).

## [0.3.2] - 2026-09-30

### Fixed

- Volumes without a name show their id instead of an empty name (#2).

## [0.3.1] - 2026-09-30

### Fixed

- Startup no longer crashes when no clouds.yaml is configured: the SDK implicit
  defaults cloud is ignored (#1).
- Swift quota and computed columns are read from the SDK resources.

### New contributors

- @zvfvrv made their first contribution in #1.

## [0.3.0] - 2026-09-30

### Added

- Swift objects browsed by folder: upload of files and directories (large files as
  static large objects), create folder, copy, edit metadata, expiry, temporary URLs,
  details with every header, delete of whole folders.
- Swift containers: storage policy, public or private access (`p` switches it), quotas
  and versioning as columns, details, delete of non empty containers.
- Swift account usage and quota in the quota panel and in the overview.

### Fixed

- Quitting no longer waits for API calls still running in the background.

## [0.2.0] - 2026-09-29

Views for what Horizon does not show to a regular user.

### Added

- Global search (`:search <text>`, `:find`, menu `S`): name, ID or IP address in
  servers, ports, floating IPs, volumes, networks, subnets, routers, security groups and
  load balancers of every project and region; `Enter` opens the result in its context.
- Unused resources (`:unused`): floating IPs not associated, volumes not attached,
  snapshots older than 30 days, servers stopped for more than 7 days, ports without a
  device, routers with a gateway and no interfaces, unused security groups. `Ctrl+D`
  deletes the highlighted one (servers excepted).
- Security group audit (`:audit`): ingress rules open to the internet for all traffic,
  all ports, sensitive services or wide port ranges, with the ports using each group.
- Security group view: number of ports using each group, `w` lists them.
- Per volume type Cinder quotas (e.g. `gigabytes_Ceph-SSD`) in the quota panel and in
  the overview, for types with a limit.
- Server groups: host count, placement check of the policy (`VIOLATED`,
  `SHARED_HOST`, `SPREAD`) and a members view with `hostId`.
- Ports: port security, security groups, allowed address pairs, trunk and QoS columns;
  actions to set allowed address pairs (`p`) and port security (`s`).
- Neutron RBAC policies (`:rbac`, `b` from a network): share a network with a project
  and stop sharing.
- Token time left in the header, warning for application credentials expiring within
  14 days, "Expires in" column in the application credentials view.
- Fault message of servers in ERROR in the server table.

## [0.1.1] - 2026-09-29

### Added

- macOS builds (`darwin-arm64`, `darwin-x86_64`).
- `install.sh`, an installer that needs no sudo.

### Fixed

- CI: pin `astral-sh/setup-uv` to an existing tag and move actions to Node 24.

## [0.1.0] - 2026-09-29

First release: k9s style terminal dashboard for OpenStack with resource views and
actions, quota panel, global overview, network topology, privacy mode and Italian
translation.

[0.3.0]: https://github.com/rizlas/ostack9s/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/rizlas/ostack9s/compare/v0.1.1...v0.2.0
[0.1.1]: https://github.com/rizlas/ostack9s/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/rizlas/ostack9s/releases/tag/v0.1.0
