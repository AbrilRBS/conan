from functools import total_ordering
from typing import Optional

from conan.internal.model.version import Version
from conan.errors import ConanException


def required_conan_version_policy(conanfile, limit_version):
    try:
        global_conf = conanfile._conan_helpers.global_conf  # noqa
    except AttributeError:
        pass  # This can happen for PLATFORM deps without _conan_helpers
    else:
        policies = global_conf.get("core:policies")
        # The global policy_conan_version one has priority
        if policies:
            policy = next(iter(p for p in policies if p.startswith("required_conan_version")), None)
            if policy:
                version = policy[len("required_conan_version"):]
                version_range = VersionRange(version)
                if not version_range.contains(Version(limit_version), resolve_prerelease=None):
                    return True

    conanfile_version = conanfile._conan_required_version # noqa
    if conanfile_version:
        version_range = VersionRange(conanfile_version)
        if not version_range.contains(Version(limit_version), resolve_prerelease=None):
            return True
    return False


@total_ordering
class _Condition:
    __slots__ = ("operator", "display_version", "version")

    def __init__(self, operator, version):
        self.operator = operator
        self.display_version = version

        value = str(version)
        if (operator == ">=" or operator == "<") and "-" not in value and version.build is None:
            value += "-"
        self.version = Version(value)

    def __str__(self):
        if self.operator == "*":  # Patterns like "1.2.*" are stored as the "1.2." prefix
            return f"{self.display_version}*"
        return f"{self.operator}{self.display_version}"

    def __repr__(self):
        return self.__str__()

    def __hash__(self):
        return hash((self.operator, self.version))

    def __lt__(self, other):
        # Notice that this is done on the modified version, might contain extra prereleases
        if self.version < other.version:
            return True
        elif self.version == other.version:
            if self.operator == "<":
                if other.operator == "<":
                    return self.display_version.pre is not None
                else:
                    return True
            elif self.operator == "<=":
                if other.operator == "<":
                    return False
                else:
                    return self.display_version.pre is None
            elif self.operator == ">":
                if other.operator == ">":
                    return self.display_version.pre is None
                else:
                    return False
            else:
                if other.operator == ">":
                    return True
                # There's a possibility of getting here while validating if a range is non-void
                # by comparing >= & <= for lower limit <= upper limit
                elif other.operator == "<=":
                    return True
                else:
                    return self.display_version.pre is not None
        return False

    def __eq__(self, other):
        return (self.display_version == other.display_version and
                self.operator == other.operator)


class _ConditionSet:

    def __init__(self, expression, prerelease):
        expressions = expression.split()
        if not expressions:
            # Guarantee at least one expression
            expressions = [""]

        self.prerelease = prerelease
        self.conditions = []
        for e in expressions:
            e = e.strip()
            self.conditions.extend(self._parse_expression(e))

    @classmethod
    def from_conditions(cls, conditions, prerelease):
        """ Build a condition set from already parsed conditions, without reparsing """
        result = cls.__new__(cls)
        result.prerelease = prerelease
        result.conditions = conditions
        return result

    @staticmethod
    def _parse_expression(expression):
        if expression in ("", "*"):
            return [_Condition(">=", Version("0.0.0"))]
        elif len(expression) == 1:
            raise ConanException(f'Error parsing version range "{expression}"')

        operator = expression[0]
        if operator not in (">", "<", "^", "~", "="):
            if expression[-1] == "*":  # Handle patterns like "1.2.*"
                operator = "*"
                expression = expression[:-1]
            else:
                operator = "="
            index = 0
        else:
            index = 1
        if operator in (">", "<"):
            if expression[1] == "=":
                operator += "="
                index = 2
        elif expression[1] == "=":
            raise ConanException(f"Invalid version range operator '{operator}=' in {expression}, you should probably use {operator} instead.")
        version = expression[index:]
        if version == "":
            raise ConanException(f'Error parsing version range "{expression}"')
        if operator == "~":  # tilde minor
            if "-" not in version:
                version += "-"
            v = Version(version)
            index = 1 if len(v.main) > 1 else 0
            return [_Condition(">=", v), _Condition("<", v.upper_bound(index))]
        elif operator == "^":  # caret major
            v = Version(version)

            def first_non_zero(main):
                for i, m in enumerate(main):
                    if m != 0:
                        return i
                return len(main)

            initial_index = first_non_zero(v.main)
            return [_Condition(">=", v), _Condition("<", v.upper_bound(initial_index))]
        else:
            return [_Condition(operator, Version(version))]

    def valid(self, version, conf_resolve_prepreleases):
        if version.pre:
            # Follow the expression desires only if core.version_ranges:resolve_prereleases is None,
            # else force to the conf's value
            if conf_resolve_prepreleases is None:
                if not self.prerelease:
                    return False
            elif conf_resolve_prepreleases is False:
                return False
        for condition in self.conditions:
            if condition.operator == ">":
                if not version > condition.version:
                    return False
            elif condition.operator == "<":
                if not version < condition.version:
                    return False
            elif condition.operator == ">=":
                if not version >= condition.version:
                    return False
            elif condition.operator == "<=":
                if not version <= condition.version:
                    return False
            elif condition.operator == "=":
                if not version == condition.version:
                    return False
            elif condition.operator == "*":
                if not str(version).startswith(str(condition.version)):
                    return False
        return True


class VersionRange:
    def __init__(self, expression):
        if not expression:
            raise ConanException(f'Error parsing empty version range')
        self._expression = expression
        tokens = expression.split(",")
        prereleases = False
        for t in tokens[1:]:
            if "include_prerelease" in t:
                if "include_prerelease=" in t:
                    from conan.api.output import ConanOutput
                    ConanOutput().warning(
                        f'include_prerelease version range option in "{expression}" does not take an attribute, '
                        'its presence unconditionally enables prereleases')
                prereleases = True
                break
            else:
                t = t.strip()
                if len(t) > 0 and t[0].isalpha():
                    from conan.api.output import ConanOutput
                    ConanOutput().warning(f'Unrecognized version range option "{t}" in "{expression}"')
                else:
                    raise ConanException(f'"{t}" in version range "{expression}" is not a valid option')
        version_expr = tokens[0]
        self.condition_sets = []
        for alternative in version_expr.split("||"):
            self.condition_sets.append(_ConditionSet(alternative, prereleases))

    def __str__(self):
        return self._expression

    def contains(self, version: Version, resolve_prerelease: Optional[bool]):
        """
        Whether <version> is inside the version range

        :param version: Version to check against
        :param resolve_prerelease: If ``True``, ensure prereleases can be resolved in this range
        If ``False``, prerelases can NOT be resolved in this range
        If ``None``, prereleases are resolved only if this version range expression says so
        :return: Whether the version is inside the range
        """
        assert isinstance(version, Version), type(version)
        for condition_set in self.condition_sets:
            if condition_set.valid(version, resolve_prerelease):
                return True
        return False

    def intersection(self, other):
        conditions = []

        def _calculate_limits(operator, candidates):
            limits = [c for c in candidates if operator in c.operator]
            return sorted(limits, reverse=operator == ">")[0] if limits else None

        def _pattern_limits(pattern):
            # "1.2.*" (stored as the "1.2." prefix) is within ">=1.2 <1.3", but that range also
            # contains "1.2" (and Version("1.2") == Version("1.2.0"), so ">1.2" isn't an option),
            # so the pattern itself must be kept too. Other patterns ("1.2*", "1.2.3+4.*"...) don't
            # have limits
            prefix = str(pattern.display_version)
            items = prefix[:-1].split(".")
            if not prefix.endswith(".") or not all(i.isdigit() for i in items):
                return []
            upper = ".".join(items[:-1] + [str(int(items[-1]) + 1)])
            return [_Condition(">=", Version(prefix[:-1])), _Condition("<", Version(upper))]

        prerelease = True
        for lhs_conditions in self.condition_sets:
            for rhs_conditions in other.condition_sets:
                all_conditions = lhs_conditions.conditions + rhs_conditions.conditions
                exact = next((c for c in all_conditions if c.operator == "="), None)
                patterns = [c for c in all_conditions if c.operator == "*"]
                # Patterns match by string prefix, so they only intersect if all of them are a
                # prefix of the longest one, and then that one is the only one needed
                pattern = max(patterns, key=lambda p: len(str(p.display_version)), default=None)
                if exact is not None:
                    # The only possible version is the exact one, as long as every other condition
                    # (other "=" ones included) accepts it. Prereleases are decided for the whole
                    # range below, so don't filter them here
                    all_set = _ConditionSet.from_conditions(all_conditions, prerelease=True)
                    if all_set.valid(exact.version, conf_resolve_prepreleases=True):
                        conditions.append([exact])
                elif all(str(pattern.display_version).startswith(str(p.display_version))
                         for p in patterns):
                    # The pattern limits narrow the range and detect empty ones. They go last, so on
                    # ties the explicit ones win (">=1.0" over the ">=1" of "1.*")
                    limits = [c for c in all_conditions if c.operator != "*"]
                    for p in patterns:
                        limits.extend(_pattern_limits(p))
                    lower_limit = _calculate_limits(">", limits)
                    upper_limit = _calculate_limits("<", limits)
                    kept_pattern = [pattern] if pattern is not None else []

                    if lower_limit and upper_limit:
                        if lower_limit <= upper_limit:
                            conditions.append([lower_limit, upper_limit] + kept_pattern)
                    elif lower_limit:
                        conditions.append([lower_limit] + kept_pattern)
                    elif upper_limit:
                        conditions.append([upper_limit] + kept_pattern)
                    elif kept_pattern:
                        conditions.append(kept_pattern)

                # conservative approach: if any of the conditions forbid prereleases, forbid them in the result
                if not lhs_conditions.prerelease or not rhs_conditions.prerelease:
                    prerelease = False

        if not conditions:
            return None
        # Don't use __init__ because it will parse the expression again, we already have the conditions
        result = VersionRange.__new__(VersionRange)
        result._expression = ' || '.join(' '.join(str(c) for c in cs) for cs in conditions) + (', include_prerelease' if prerelease else '')
        result.condition_sets = [_ConditionSet.from_conditions(cs, prerelease) for cs in conditions]
        return result

    def version(self):
        return Version(f"[{self._expression}]")


def validate_conan_version(required_range):
    from conan import __version__  # To avoid circular imports
    clientver = Version(__version__)
    version_range = VersionRange(required_range)
    for conditions in version_range.condition_sets:
        conditions.prerelease = True
    if not version_range.contains(clientver, resolve_prerelease=None):
        raise ConanException("Current Conan version ({}) does not satisfy "
                             "the defined one ({}).".format(clientver, required_range))
