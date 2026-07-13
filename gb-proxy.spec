Name:           gb-proxy
Version:        0.1.0
Release:        1%{?dist}
Summary:        HTTP proxy for GEOBENCH and other legacy web clients

License:        BSD-3-Clause
URL:            https://github.com/salvogendut/GB-proxy
Source0:        %{url}/archive/v%{version}/GB-proxy-%{version}.tar.gz
Source1:        https://raw.githubusercontent.com/salvogendut/GB-proxy/v%{version}/packaging/%{name}.sysusers

BuildArch:      noarch
BuildRequires:  python3-devel
BuildRequires:  pyproject-rpm-macros
BuildRequires:  systemd-rpm-macros
%{?sysusers_requires_compat}

%description
GB-proxy is an extensible HTTP proxy that adapts modern web content for
GEOBENCH and other early computers. It simplifies HTML, rewrites links, and
converts images into formats suitable for constrained legacy web clients.


%generate_buildrequires
%pyproject_buildrequires -r


%prep
%autosetup -n GB-proxy-%{version}


%build
%pyproject_wheel


%install
%pyproject_install
%pyproject_save_files -l '*'

install -Dpm 0644 packaging/%{name}.service %{buildroot}%{_unitdir}/%{name}.service
install -Dpm 0644 packaging/%{name}.sysconfig %{buildroot}%{_sysconfdir}/sysconfig/%{name}
install -Dpm 0644 %{SOURCE1} %{buildroot}%{_sysusersdir}/%{name}.conf
install -Dpm 0644 packaging/%{name}.1 %{buildroot}%{_mandir}/man1/%{name}.1
install -Dpm 0640 config.py.example %{buildroot}%{_sysconfdir}/%{name}/config.py


%check
%{python3} -m unittest discover -s tests -v


%if 0%{?rhel} == 9
%pre
%sysusers_create_compat %{SOURCE1}
%endif


%post
%systemd_post %{name}.service


%preun
%systemd_preun %{name}.service


%postun
%systemd_postun_with_restart %{name}.service


%files -f %{pyproject_files}
%doc README.md
%{_bindir}/%{name}
%dir %{_sysconfdir}/%{name}
%config(noreplace) %attr(0640,root,gb-proxy) %{_sysconfdir}/%{name}/config.py
%config(noreplace) %{_sysconfdir}/sysconfig/%{name}
%{_unitdir}/%{name}.service
%{_sysusersdir}/%{name}.conf
%{_mandir}/man1/%{name}.1*


%changelog
* Mon Jul 13 2026 Salvatore Bognanni <salvogendut@users.noreply.github.com> - 0.1.0-1
- Initial RPM package with a hardened systemd service
