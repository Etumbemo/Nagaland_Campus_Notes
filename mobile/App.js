import { useCallback, useEffect, useMemo, useState } from 'react';
import {
  Alert,
  Image,
  Linking,
  Modal,
  Platform,
  Pressable,
  ScrollView,
  StyleSheet,
  Text,
  TextInput,
  View,
  useWindowDimensions,
} from 'react-native';
import * as DocumentPicker from 'expo-document-picker';
import * as FileSystem from 'expo-file-system/legacy';
import * as IntentLauncher from 'expo-intent-launcher';
import * as Sharing from 'expo-sharing';
import { SafeAreaProvider, SafeAreaView } from 'react-native-safe-area-context';
import {
  request, getStoredUser, register, signIn, signOut, uploadPdf, requestLoginOtp, confirmLoginOtp,
  uploadStudyResource, downloadStudyResource, downloadPaperFile,
  requestPasswordReset, confirmPasswordReset,
  requestEmailVerification, confirmEmailVerification,
} from './api';
import AIPreparationScreen from './AIPreparationScreen';

import { COLORS } from './src/constants/colors';
const EXAM_TYPES = ['Weekly Test 1', 'Weekly Test 2', 'CIA 1', 'CIA 2', 'End Semester'];
const PASSWORD_REQUIREMENTS = 'At least 8 characters, including one uppercase letter and one special symbol.';
let themedAlertDispatcher = null;

function showThemedAlert(title, message, buttons) {
  if (themedAlertDispatcher) {
    themedAlertDispatcher(title, message, buttons);
  } else {
    Alert.alert(title, message, buttons);
  }
}

function isPasswordValid(password) {
  return password.length >= 8
    && /[A-Z]/.test(password)
    && /[^A-Za-z0-9\s]/.test(password);
}

function isEmailValid(email) {
  return /^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(email.trim());
}

function isRegistrationTextValid(value) {
  return value.trim().length >= 2 && /[\p{L}\p{N}]/u.test(value);
}

function Field({
  label, value, onChangeText, placeholder, keyboardType, secureTextEntry, multiline, autoCapitalize, maxLength,
  showPasswordToggle, passwordVisible, onTogglePasswordVisibility,
}) {
  return (
    <View style={styles.field}>
      {label ? <Text style={styles.label}>{label}</Text> : null}
      <View style={showPasswordToggle && styles.passwordInputRow}>
        <TextInput
          value={value}
          onChangeText={onChangeText}
          placeholder={placeholder}
          placeholderTextColor="#8a95a7"
          keyboardType={keyboardType}
          maxLength={maxLength}
          secureTextEntry={secureTextEntry && !passwordVisible}
          multiline={multiline}
          autoCapitalize={autoCapitalize}
          style={[
            styles.input,
            multiline && styles.multiline,
            showPasswordToggle && styles.passwordInput,
          ]}
        />
        {showPasswordToggle ? (
          <Pressable
            onPress={onTogglePasswordVisibility}
            accessibilityRole="button"
            accessibilityLabel={passwordVisible ? 'Hide password' : 'Show password'}
            style={styles.passwordToggle}
          >
            <Text style={styles.passwordToggleText}>{passwordVisible ? 'Hide' : 'Show'}</Text>
          </Pressable>
        ) : null}
      </View>
    </View>
  );
}

function ActionButton({ title, onPress, disabled, secondary, compact }) {
  return (
    <Pressable
      disabled={disabled}
      onPress={onPress}
      style={({ pressed }) => [
        styles.button,
        secondary && styles.secondaryButton,
        compact && styles.compactButton,
        disabled && styles.disabledButton,
        pressed && !disabled && styles.buttonPressed,
      ]}
    >
      <Text style={[styles.buttonText, secondary && styles.secondaryButtonText]}>
        {title}
      </Text>
    </Pressable>
  );
}

function ThemedSelect({ label, value, options, onChange, disabled }) {
  const [visible, setVisible] = useState(false);
  const selected = options.find((option) => option.value === value);

  return (
    <View style={styles.field}>
      <Text style={styles.label}>{label}</Text>
      <Pressable
        accessibilityRole="button"
        accessibilityLabel={`${label}: ${selected?.label || 'Choose an option'}`}
        disabled={disabled || options.length === 0}
        onPress={() => setVisible(true)}
        style={({ pressed }) => [
          styles.selectButton,
          pressed && styles.buttonPressed,
          (disabled || options.length === 0) && styles.disabledButton,
        ]}
      >
        <Text style={selected ? styles.selectButtonText : styles.selectPlaceholder}>
          {selected?.label || 'No options available'}
        </Text>
        <Text style={styles.selectChevron}>⌄</Text>
      </Pressable>
      <Modal
        visible={visible}
        transparent
        animationType="fade"
        onRequestClose={() => setVisible(false)}
      >
        <Pressable style={styles.selectBackdrop} onPress={() => setVisible(false)}>
          <Pressable style={styles.selectDialog} onPress={(event) => event.stopPropagation()}>
            <Text style={styles.dialogTitle}>{label}</Text>
            <ScrollView style={styles.selectOptions} keyboardShouldPersistTaps="handled">
              {options.map((option) => {
                const active = option.value === value;
                return (
                  <Pressable
                    key={option.value}
                    accessibilityRole="button"
                    accessibilityState={{ selected: active }}
                    onPress={() => {
                      onChange(option.value);
                      setVisible(false);
                    }}
                    style={[styles.selectOption, active && styles.selectOptionActive]}
                  >
                    <Text style={[styles.selectOptionText, active && styles.selectOptionTextActive]}>
                      {option.label}
                    </Text>
                    {active ? <Text style={styles.selectCheck}>✓</Text> : null}
                  </Pressable>
                );
              })}
            </ScrollView>
            <ActionButton title="Cancel" secondary onPress={() => setVisible(false)} />
          </Pressable>
        </Pressable>
      </Modal>
    </View>
  );
}

function Notice({ message, onDismiss }) {
  if (!message) return null;
  return (
    <Pressable onPress={onDismiss} style={styles.notice}>
      <Text style={styles.noticeText}>{message}</Text>
      <Text style={styles.noticeDismiss}>Dismiss</Text>
    </Pressable>
  );
}

function PaperCard({ paper, favorite, onFavorite, onOpen, onDownload, onShare }) {
  return (
    <View style={styles.card}>
      <View style={styles.rowBetween}>
        <Text style={styles.cardTitle}>{paper.subject}</Text>
        <Pressable onPress={onFavorite} accessibilityLabel="Toggle favorite" hitSlop={8} style={styles.favButton}>
          <Text style={styles.favoriteMint}>{favorite ? '★' : '☆'}</Text>
        </Pressable>
      </View>
      <View style={styles.pillRow}>
        <View style={styles.deptBadge}>
          <Text style={styles.deptBadgeText}>{paper.departmentName || 'Dept'}</Text>
        </View>
        <View style={styles.metaPill}>
          <Text style={styles.metaPillText}>Sem {paper.semester}</Text>
        </View>
        <View style={styles.metaPill}>
          <Text style={styles.metaPillText}>{paper.examType}</Text>
        </View>
        <View style={styles.metaPill}>
          <Text style={styles.metaPillText}>{paper.year}</Text>
        </View>
      </View>
      {paper.subjectCode ? <Text style={styles.subjectCodeText}>Code: {paper.subjectCode}</Text> : null}
      {paper.description ? <Text style={styles.paperDescription} numberOfLines={2}>{paper.description}</Text> : null}
      <View style={styles.paperMetricsRow}>
        <Text style={styles.metricText}>👁️ {paper.views} views</Text>
        <Text style={styles.metricDivider}>•</Text>
        <Text style={styles.metricText}>⬇️ {paper.downloads} downloads</Text>
      </View>
      <View style={styles.actions}>
        <ActionButton title="View PDF" compact onPress={onOpen} disabled={!paper.fileUrl} />
        <ActionButton title="Download" compact secondary onPress={onDownload} disabled={!paper.fileUrl} />
        <ActionButton title="Share" compact secondary onPress={onShare} disabled={!paper.fileUrl} />
      </View>
    </View>
  );
}

function HomeScreen({
  papers,
  departments,
  user,
  favorites,
  onFavorite,
  onBrowse,
  onOpenPaper,
  onDownloadPaper,
  onSharePaper,
  onAIPreparation,
  compact,
}) {
  const [searchQuery, setSearchQuery] = useState('');
  const [selectedDept, setSelectedDept] = useState('');

  const filteredPapers = useMemo(() => {
    let result = [...papers].sort((a, b) => new Date(b.createdAt) - new Date(a.createdAt));
    if (selectedDept) {
      result = result.filter((p) => p.departmentId === selectedDept);
    }
    if (searchQuery.trim()) {
      const q = searchQuery.trim().toLowerCase();
      result = result.filter((p) =>
        `${p.subject} ${p.subjectCode || ''} ${p.description || ''} ${p.departmentName || ''}`
          .toLowerCase()
          .includes(q),
      );
    }
    return result;
  }, [papers, selectedDept, searchQuery]);

  const displayPapers = useMemo(() => {
    if (!searchQuery.trim() && !selectedDept) {
      return filteredPapers.slice(0, 6);
    }
    return filteredPapers;
  }, [filteredPapers, searchQuery, selectedDept]);

  const totalDownloads = useMemo(
    () => papers.reduce((sum, p) => sum + (p.downloads || 0), 0),
    [papers],
  );

  return (
    <View style={styles.homeContainer}>
      <ScrollView contentContainerStyle={styles.homeScrollContent} showsVerticalScrollIndicator={false}>
        {/* Welcome Glass Banner */}
        <View style={[styles.heroGlass, compact && styles.heroCompact]}>
          <View style={styles.eyebrowContainer}>
            <Text style={styles.eyebrow}>CAMPUS NOTES</Text>
            <View style={styles.eyebrowDot} />
            <Text style={styles.eyebrowLocation}>NAGALAND</Text>
          </View>
          <Text style={[styles.heroTitle, compact && styles.heroTitleCompact]}>
            {user?.name ? `Welcome back,\n${user.name}` : 'College Study Notes &\nQuestion Papers'}
          </Text>
          <Text style={styles.heroCopy}>
            Access, read, and download official previous semester examination papers across Nagaland colleges and departments.
          </Text>
          <View style={styles.heroActionRow}>
            <ActionButton title="Explore All Papers →" onPress={() => onBrowse('')} />
          </View>
        </View>

        <Pressable
          accessibilityRole="button"
          onPress={onAIPreparation}
          style={styles.aiPreparationHomeCard}
        >
          <Text style={styles.aiPreparationEyebrow}>AI EXAM PREPARATION</Text>
          <Text style={styles.aiPreparationTitle}>Practice from previous-year papers</Text>
          <Text style={styles.aiPreparationCopy}>
            Generate a mock test or a prioritized list of important questions.
          </Text>
          <Text style={styles.aiPreparationLink}>Get started →</Text>
        </Pressable>

        {/* Home Search Bar */}
        <View style={styles.homeSearchContainer}>
          <Text style={styles.searchIcon}>🔍</Text>
          <TextInput
            value={searchQuery}
            onChangeText={setSearchQuery}
            placeholder="Search by subject, code, or department..."
            placeholderTextColor={COLORS.softGrey}
            style={styles.homeSearchInput}
            autoCapitalize="none"
          />
          {searchQuery ? (
            <Pressable onPress={() => setSearchQuery('')} hitSlop={8} style={styles.searchClearBtn}>
              <Text style={styles.searchClearText}>✕</Text>
            </Pressable>
          ) : null}
        </View>

        {/* Dynamic Statistics Deck */}
        <View style={styles.statsRow}>
          <View style={styles.statCard}>
            <Text style={styles.statValue}>{papers.length}</Text>
            <Text style={styles.statLabel}>Papers</Text>
          </View>
          <View style={styles.statCard}>
            <Text style={styles.statValue}>{departments.length}</Text>
            <Text style={styles.statLabel}>Departments</Text>
          </View>
          <View style={styles.statCard}>
            <Text style={styles.statValue}>{totalDownloads}</Text>
            <Text style={styles.statLabel}>Downloads</Text>
          </View>
        </View>

        {/* Department Filter Chips */}
        <View style={styles.deptSection}>
          <View style={styles.sectionHeaderRow}>
            <Text style={styles.sectionTitle}>Departments</Text>
            {selectedDept ? (
              <Pressable onPress={() => setSelectedDept('')} hitSlop={8}>
                <Text style={styles.clearFilterLink}>Show all</Text>
              </Pressable>
            ) : null}
          </View>
          <ScrollView
            horizontal
            showsHorizontalScrollIndicator={false}
            contentContainerStyle={styles.deptScroll}
          >
            <Pressable
              onPress={() => setSelectedDept('')}
              style={[styles.glassChip, !selectedDept && styles.activeGlassChip]}
            >
              <Text style={[styles.glassChipText, !selectedDept && styles.activeGlassChipText]}>
                All ({papers.length})
              </Text>
            </Pressable>
            {departments.map((dept) => {
              const count = papers.filter((p) => p.departmentId === dept.id).length;
              const isSelected = selectedDept === dept.id;
              return (
                <Pressable
                  key={dept.id}
                  onPress={() => setSelectedDept(isSelected ? '' : dept.id)}
                  style={[styles.glassChip, isSelected && styles.activeGlassChip]}
                >
                  <Text style={[styles.glassChipText, isSelected && styles.activeGlassChipText]}>
                    {dept.code || dept.name} ({count})
                  </Text>
                </Pressable>
              );
            })}
          </ScrollView>
        </View>

        {/* Paper Results Header */}
        <View style={styles.sectionHeaderRow}>
          <Text style={styles.sectionTitle}>
            {searchQuery || selectedDept
              ? `Results (${filteredPapers.length})`
              : 'Recently Added'}
          </Text>
          <Pressable onPress={() => onBrowse(selectedDept)} hitSlop={8}>
            <Text style={styles.browseAllLink}>Browse all →</Text>
          </Pressable>
        </View>

        {/* Paper Cards List */}
        {displayPapers.length ? (
          displayPapers.map((paper) => {
            const isFav = favorites?.includes(paper.id);
            return (
              <View key={paper.id} style={styles.paperGlassCard}>
                <View style={styles.paperTopRow}>
                  <Pressable
                    onPress={() => onOpenPaper(paper)}
                    style={{ flex: 1 }}
                    accessibilityRole="button"
                    accessibilityLabel={`Read ${paper.subject}`}
                  >
                    <Text style={styles.paperTitle} numberOfLines={2}>
                      {paper.subject}
                    </Text>
                  </Pressable>
                  {onFavorite ? (
                    <Pressable
                      onPress={() => onFavorite(paper.id)}
                      accessibilityLabel="Toggle favorite"
                      hitSlop={8}
                      style={styles.favButton}
                    >
                      <Text style={styles.favoriteMint}>{isFav ? '★' : '☆'}</Text>
                    </Pressable>
                  ) : null}
                </View>

                <View style={styles.pillRow}>
                  <View style={styles.deptBadge}>
                    <Text style={styles.deptBadgeText}>{paper.departmentName || 'Dept'}</Text>
                  </View>
                  <View style={styles.metaPill}>
                    <Text style={styles.metaPillText}>Sem {paper.semester}</Text>
                  </View>
                  <View style={styles.metaPill}>
                    <Text style={styles.metaPillText}>{paper.examType}</Text>
                  </View>
                  <View style={styles.metaPill}>
                    <Text style={styles.metaPillText}>{paper.year}</Text>
                  </View>
                </View>

                {paper.subjectCode ? (
                  <Text style={styles.subjectCodeText}>Code: {paper.subjectCode}</Text>
                ) : null}

                {paper.description ? (
                  <Text style={styles.paperDescription} numberOfLines={2}>
                    {paper.description}
                  </Text>
                ) : null}

                <View style={styles.paperMetricsRow}>
                  <Text style={styles.metricText}>👁️ {paper.views} views</Text>
                  <Text style={styles.metricDivider}>•</Text>
                  <Text style={styles.metricText}>⬇️ {paper.downloads} downloads</Text>
                </View>

                <View style={styles.actions}>
                  <ActionButton
                    title="View PDF"
                    compact
                    onPress={() => onOpenPaper(paper)}
                    disabled={!paper.fileUrl}
                  />
                  <ActionButton
                    title="Download"
                    compact
                    secondary
                    onPress={() => onDownloadPaper(paper)}
                    disabled={!paper.fileUrl}
                  />
                  <ActionButton
                    title="Share"
                    compact
                    secondary
                    onPress={() => onSharePaper(paper)}
                    disabled={!paper.fileUrl}
                  />
                </View>
              </View>
            );
          })
        ) : (
          <View style={styles.emptyGlass}>
            <Text style={styles.emptyIcon}>📂</Text>
            <Text style={styles.emptyTitle}>No matching question papers</Text>
            <Text style={styles.emptySub}>
              {searchQuery
                ? `No papers match "${searchQuery}".`
                : 'No papers found in this category yet.'}
            </Text>
            {(searchQuery || selectedDept) ? (
              <ActionButton
                title="Reset Filters"
                compact
                secondary
                onPress={() => {
                  setSearchQuery('');
                  setSelectedDept('');
                }}
              />
            ) : null}
          </View>
        )}
      </ScrollView>
    </View>
  );
}

function Stat({ title, value }) {
  return (
    <View style={styles.stat}>
      <Text style={styles.statValue}>{value}</Text>
      <Text style={styles.muted}>{title}</Text>
    </View>
  );
}

function BrowseScreen({
  papers, departments, user, favorites, initialDepartment, onFavorite, refresh, clearNotice, compact,
  onOpenPaper, onDownloadPaper, onSharePaper,
}) {
  const [search, setSearch] = useState('');
  const [department, setDepartment] = useState(initialDepartment || '');
  const [semester, setSemester] = useState('');
  const [examType, setExamType] = useState('');
  const [year, setYear] = useState('');
  const [favoritesOnly, setFavoritesOnly] = useState(false);
  const filtered = papers.filter((paper) => {
    const query = search.trim().toLowerCase();
    return (!query || `${paper.subject} ${paper.subjectCode || ''} ${paper.description || ''}`.toLowerCase().includes(query))
      && (!department || paper.departmentId === department)
      && (!semester || String(paper.semester) === semester)
      && (!examType || paper.examType === examType)
      && (!year || paper.year.includes(year))
      && (!favoritesOnly || favorites.includes(paper.id));
  });

  return (
    <ScrollView contentContainerStyle={styles.content} keyboardShouldPersistTaps="handled">
      <Text style={styles.screenTitle}>Browse papers</Text>
      <Field label="Search" value={search} onChangeText={setSearch} placeholder="Subject or subject code" />
      <ThemedSelect
        label="Department"
        value={department}
        onChange={setDepartment}
        options={[
          { label: 'All departments', value: '' },
          ...departments.map((item) => ({ label: `${item.name} (${item.code})`, value: item.id })),
        ]}
      />
      <View style={[styles.filterRow, compact && styles.filterColumn]}>
        <View style={styles.filterHalf}>
          <Field label="Semester" value={semester} onChangeText={(value) => setSemester(value.replace(/\D/g, '').slice(0, 2))} placeholder="All" keyboardType="number-pad" />
        </View>
        <View style={styles.filterHalf}>
          <Field label="Year" value={year} onChangeText={setYear} placeholder="All" keyboardType="number-pad" />
        </View>
      </View>
      <Text style={styles.label}>Exam type</Text>
      <View style={styles.chips}>
        <Chip title="All" selected={!examType} onPress={() => setExamType('')} />
        {EXAM_TYPES.map((item) => (
          <Chip key={item} title={item} selected={examType === item} onPress={() => setExamType(examType === item ? '' : item)} />
        ))}
      </View>
      {user ? (
        <ActionButton
          title={favoritesOnly ? 'Show all papers' : 'Show favorites'}
          secondary
          onPress={() => setFavoritesOnly(!favoritesOnly)}
        />
      ) : null}
      <View style={styles.rowBetween}>
        <Text style={styles.sectionTitle}>{filtered.length} papers</Text>
        <Pressable onPress={() => { clearNotice(); refresh(); }}>
          <Text style={styles.link}>Refresh</Text>
        </Pressable>
      </View>
      {filtered.map((paper) => (
        <PaperCard
          key={paper.id}
          paper={paper}
          favorite={favorites.includes(paper.id)}
          onFavorite={() => onFavorite(paper.id)}
          onOpen={() => onOpenPaper(paper)}
          onDownload={() => onDownloadPaper(paper)}
          onShare={() => onSharePaper(paper)}
        />
      ))}
      {!filtered.length ? <EmptyState text="No papers match these filters." /> : null}
    </ScrollView>
  );
}

function Chip({ title, selected, onPress }) {
  return (
    <Pressable onPress={onPress} style={[styles.chip, selected && styles.selectedChip]}>
      <Text style={[styles.chipText, selected && styles.selectedChipText]}>{title}</Text>
    </Pressable>
  );
}

function DepartmentsScreen({ departments, papers, onBrowse }) {
  return (
    <ScrollView contentContainerStyle={styles.content}>
      <Text style={styles.screenTitle}>Departments</Text>
      <Text style={styles.muted}>Choose a department to see its question papers.</Text>
      {departments.map((department) => (
        <Pressable key={department.id} style={styles.card} onPress={() => onBrowse(department.id)}>
          <View style={styles.rowBetween}>
            <Text style={styles.cardTitle}>{department.name}</Text>
            <Text style={styles.badge}>{department.code}</Text>
          </View>
          <Text style={styles.muted}>
            {papers.filter((paper) => paper.departmentId === department.id).length} papers · Tap to browse
          </Text>
        </Pressable>
      ))}
      {!departments.length ? <EmptyState text="No departments have been added yet." /> : null}
    </ScrollView>
  );
}

function LoginPanel({ onLogin, onRegister, onVerifyEmail, onVerifyLogin, loading }) {
  const [mode, setMode] = useState('choose');
  const [name, setName] = useState('');
  const [email, setEmail] = useState('');
  const [password, setPassword] = useState('');
  const [loginEmailError, setLoginEmailError] = useState('');
  const [loginPasswordError, setLoginPasswordError] = useState('');
  const [confirmPassword, setConfirmPassword] = useState('');
  const [passwordVisible, setPasswordVisible] = useState(false);
  const [confirmPasswordVisible, setConfirmPasswordVisible] = useState(false);
  const [accountType, setAccountType] = useState(null);
  const [instituteName, setInstituteName] = useState('');
  const [phone, setPhone] = useState('');
  const [designation, setDesignation] = useState('');
  const [supportingDocument, setSupportingDocument] = useState(null);
  const [otp, setOtp] = useState('');
  const [verificationCode, setVerificationCode] = useState('');
  const [newPassword, setNewPassword] = useState('');
  const [newPasswordVisible, setNewPasswordVisible] = useState(false);
  const [resendSeconds, setResendSeconds] = useState(0);
  const [resetBusy, setResetBusy] = useState(false);
  const isRegistering = mode === 'register';
  const isRequestingReset = mode === 'forgot';
  const isConfirmingReset = mode === 'reset';
  const isVerifyingEmail = mode === 'verify_email';
  const isVerifyingLogin = mode === 'verify_login';
  const isChoosingAccountType = mode === 'choose';
  const accountTypeLabel = accountType === 'institute' ? 'Institute staff' : 'Reader';

  function chooseAccountType(type) {
    setAccountType(type);
    setMode('login');
  }

  async function chooseSupportingDocument() {
    try {
      const result = await DocumentPicker.getDocumentAsync({
        type: ['application/pdf', 'image/jpeg', 'image/png'],
        copyToCacheDirectory: true,
      });
      if (!result.canceled) {
        const document = result.assets[0];
        const extension = document.name.split('.').pop()?.toLowerCase();
        if (!['pdf', 'jpg', 'jpeg', 'png'].includes(extension)) {
          showThemedAlert('Unsupported document', 'Choose a PDF, JPG, or PNG supporting document.');
          return;
        }
        if (document.size !== undefined && document.size <= 0) {
          showThemedAlert('Empty document', 'Choose a supporting document that is not empty.');
          return;
        }
        if (document.size !== undefined && document.size > 20 * 1024 * 1024) {
          showThemedAlert('Document too large', 'The supporting document must be 20 MB or smaller.');
          return;
        }
        setSupportingDocument(document);
      }
    } catch (error) {
      showThemedAlert('Could not open documents', error.message);
    }
  }

  useEffect(() => {
    if (resendSeconds <= 0) return undefined;
    const timer = setTimeout(() => setResendSeconds((seconds) => seconds - 1), 1000);
    return () => clearTimeout(timer);
  }, [resendSeconds]);

  async function sendResetCode() {
    setResetBusy(true);
    try {
      const result = await requestPasswordReset(email.trim());
      setResendSeconds(result.resendAfterSeconds || 60);
      setMode('reset');
      showThemedAlert('Check your email', result.message);
    } catch (error) {
      showThemedAlert('Could not send reset code', error.message);
    } finally {
      setResetBusy(false);
    }
  }

  async function resetPassword() {
    setResetBusy(true);
    try {
      const result = await confirmPasswordReset(email.trim(), otp, newPassword);
      setMode('login');
      setPassword('');
      setNewPassword('');
      setOtp('');
      showThemedAlert('Password updated', result.message);
    } catch (error) {
      showThemedAlert('Could not reset password', error.message);
    } finally {
      setResetBusy(false);
    }
  }

  async function submitRegistration() {
    try {
      const result = await onRegister(
        name.trim(), email.trim(), password, accountType, instituteName.trim(), {
          phone: phone.trim(),
          designation: designation.trim(),
          supportingDocument,
        },
      );
      setVerificationCode('');
      setResendSeconds(result.resendAfterSeconds || 60);
      setMode('verify_email');
      showThemedAlert('Check your email', result.message);
    } catch (error) {
      showThemedAlert('Could not create account', error.message);
    }
  }

  async function resendVerificationCode() {
    setResetBusy(true);
    try {
      const result = await requestEmailVerification(email.trim());
      setResendSeconds(result.resendAfterSeconds || 60);
      showThemedAlert('Check your email', result.message);
    } catch (error) {
      showThemedAlert('Could not send verification code', error.message);
    } finally {
      setResetBusy(false);
    }
  }

  async function verifyEmail() {
    setResetBusy(true);
    try {
      await onVerifyEmail(email.trim(), verificationCode);
      setMode('choose');
      setVerificationCode('');
      setPassword('');
    } catch (error) {
      showThemedAlert('Could not verify email', error.message);
    } finally {
      setResetBusy(false);
    }
  }

  async function submitLogin() {
    const normalizedEmail = email.trim();
    const emailError = !normalizedEmail
      ? 'Email is required.'
      : !isEmailValid(normalizedEmail)
        ? 'Enter a valid email address.'
        : '';
    const passwordError = password ? '' : 'Password is required.';
    setLoginEmailError(emailError);
    setLoginPasswordError(passwordError);
    if (emailError || passwordError) return;

    try {
      const result = await onLogin(normalizedEmail, password, accountType);
      if (result?.otpRequired) {
        setVerificationCode('');
        setResendSeconds(result.resendAfterSeconds || 60);
        setMode('verify_login');
        showThemedAlert('Check your email', result.message);
      }
    } catch (error) {
      if (error.message.startsWith('This email is registered as ')) {
        setLoginEmailError(error.message);
      } else if (error.message.startsWith('Verify your email')) {
        setResendSeconds(0);
        setMode('verify_email');
      } else if (error.message === 'Invalid email or password') {
        setLoginPasswordError('Email or password is incorrect.');
      } else {
        setLoginPasswordError(error.message);
      }
    }
  }

  async function verifyLogin() {
    setResetBusy(true);
    try {
      await onVerifyLogin(email.trim(), verificationCode, accountType);
    } catch (error) {
      showThemedAlert('Could not sign in', error.message);
    } finally {
      setResetBusy(false);
    }
  }

  async function resendLoginCode() {
    setResetBusy(true);
    try {
      const result = await requestLoginOtp(email.trim(), password);
      setResendSeconds(result.resendAfterSeconds || 60);
      showThemedAlert('Check your email', result.message);
    } catch (error) {
      showThemedAlert('Could not send sign-in code', error.message);
    } finally {
      setResetBusy(false);
    }
  }

  return (
    <View style={styles.card}>
      {isChoosingAccountType ? (
        <>
          <Text style={styles.cardTitle}>Choose your account type</Text>
          <Text style={styles.muted}>Select the account you want to sign in to or create.</Text>
          <ActionButton title="Reader" onPress={() => chooseAccountType('user')} />
          <Text style={styles.muted}>Browse papers and save favorites.</Text>
          <ActionButton title="Institute staff" secondary onPress={() => chooseAccountType('institute')} />
          <Text style={styles.muted}>Request access to upload and manage institute papers.</Text>
        </>
      ) : null}
      {!isChoosingAccountType ? (
        <>
      <Text style={styles.cardTitle}>
        {isVerifyingEmail || isVerifyingLogin
          ? 'Verify your email'
          : isRegistering
          ? `Create ${accountTypeLabel.toLowerCase()} account`
          : isRequestingReset
            ? 'Forgot password'
            : isConfirmingReset
              ? 'Reset password'
              : `${accountTypeLabel} sign in`}
      </Text>
      <Text style={styles.muted}>
        {isVerifyingEmail || isVerifyingLogin
          ? `Enter the six-digit code sent to ${email}.`
          : isRegistering
          ? accountType === 'institute'
            ? 'Create an institute staff account. Uploading and editing become available after an administrator approves your account.'
            : 'Create a reader account to browse papers, save favorites, and request permission to upload study resources.'
          : isRequestingReset || isConfirmingReset
            ? 'Password reset is available for reader, institute, and administrator accounts.'
            : accountType === 'institute'
              ? 'Sign in to check your institute approval status and manage papers when approved.'
              : 'Sign in to browse papers, manage favorites, and request permission to upload notes or assignments.'}
      </Text>
      {!isVerifyingEmail && !isVerifyingLogin ? <Pressable
        accessibilityRole="button"
        disabled={loading || resetBusy}
        onPress={() => setMode('choose')}
        style={styles.authSwitch}
      >
        <Text style={styles.link}>Change account type · {accountTypeLabel}</Text>
      </Pressable> : null}
      {isVerifyingEmail || isVerifyingLogin ? (
        <>
          <Field
            label="Email"
            value={email}
            editable={!isVerifyingLogin}
            onChangeText={setEmail}
            placeholder="you@example.com"
            keyboardType="email-address"
            autoCapitalize="none"
            maxLength={254}
          />
          <Field
            label="Email code"
            value={verificationCode}
            onChangeText={(value) => setVerificationCode(value.replace(/\D/g, '').slice(0, 6))}
            placeholder="6-digit code"
            keyboardType="number-pad"
          />
          <ActionButton
            title={resetBusy ? 'Verifying…' : (isVerifyingLogin ? 'Sign in' : 'Verify email')}
            onPress={isVerifyingLogin ? verifyLogin : verifyEmail}
            disabled={resetBusy || verificationCode.length !== 6 || !email.trim()}
          />
          {isVerifyingLogin ? <ActionButton
            title={resendSeconds ? `Resend code in ${resendSeconds}s` : (resetBusy ? 'Sending…' : 'Resend code')}
            onPress={resendLoginCode}
            disabled={resetBusy || resendSeconds > 0}
            secondary
          /> : <ActionButton
            title={resendSeconds ? `Resend code in ${resendSeconds}s` : (resetBusy ? 'Sending…' : 'Resend code')}
            onPress={resendVerificationCode}
            disabled={resetBusy || resendSeconds > 0 || !email.trim()}
            secondary
          />}
          <Pressable disabled={resetBusy} onPress={() => setMode(isVerifyingLogin ? 'login' : 'register')} style={styles.authSwitch}>
            <Text style={styles.link}>{isVerifyingLogin ? 'Back to sign in' : 'Back to registration'}</Text>
          </Pressable>
        </>
      ) : null}
      {isRequestingReset || isConfirmingReset ? (
        <>
          <Field label="Email" value={email} onChangeText={setEmail} placeholder="you@example.com" keyboardType="email-address" autoCapitalize="none" />
          {isConfirmingReset ? (
            <>
              <Field label="Email code" value={otp} onChangeText={(value) => setOtp(value.replace(/\D/g, '').slice(0, 6))} placeholder="6-digit code" keyboardType="number-pad" />
              <Field
                label="New password"
                value={newPassword}
                onChangeText={setNewPassword}
                placeholder="Create a strong password"
                secureTextEntry
                showPasswordToggle
                passwordVisible={newPasswordVisible}
                onTogglePasswordVisibility={() => setNewPasswordVisible((visible) => !visible)}
              />
              <Text style={styles.passwordHint}>{PASSWORD_REQUIREMENTS}</Text>
              <ActionButton
                title={resetBusy ? 'Updating password…' : 'Update password'}
                onPress={resetPassword}
                disabled={resetBusy || otp.length !== 6 || !isPasswordValid(newPassword) || !email.trim()}
              />
              <ActionButton
                title={resendSeconds ? `Resend code in ${resendSeconds}s` : (resetBusy ? 'Sending…' : 'Resend code')}
                onPress={sendResetCode}
                disabled={resetBusy || resendSeconds > 0 || !email.trim()}
                secondary
              />
            </>
          ) : (
            <ActionButton
              title={resetBusy ? 'Sending code…' : 'Send reset code'}
              onPress={sendResetCode}
              disabled={resetBusy || !email.trim()}
            />
          )}
          <Pressable disabled={resetBusy} onPress={() => setMode('login')} style={styles.authSwitch}>
            <Text style={styles.link}>Back to sign in</Text>
          </Pressable>
        </>
      ) : null}
      {!isRequestingReset && !isConfirmingReset && !isVerifyingEmail && !isVerifyingLogin ? (
        <>
          {isRegistering ? (
            <>
              <Field label="Full name" value={name} onChangeText={setName} placeholder="Your name" maxLength={160} />
              {name.length > 0 && !isRegistrationTextValid(name) ? (
                <Text style={styles.errorText}>Enter a name with at least 2 letters or numbers.</Text>
              ) : null}
            </>
          ) : null}
          {isRegistering ? (
            <>
              {accountType === 'institute' ? (
                <>
                  <Field
                    label="Phone number"
                    value={phone}
                    onChangeText={(value) => setPhone(value.replace(/\D/g, '').slice(0, 10))}
                    placeholder="10-digit phone number"
                    keyboardType="number-pad"
                    maxLength={10}
                  />
                  {phone.length > 0 && phone.length !== 10 ? (
                    <Text style={styles.errorText}>Phone number must contain exactly 10 digits.</Text>
                  ) : null}
                  <Field
                    label="Designation"
                    value={designation}
                    onChangeText={setDesignation}
                    placeholder="e.g. Lecturer"
                    maxLength={120}
                  />
                  {designation.length > 0 && !isRegistrationTextValid(designation) ? (
                    <Text style={styles.errorText}>Enter a valid designation (at least 2 characters).</Text>
                  ) : null}
                  <Field
                    label="College / university name"
                    value={instituteName}
                    onChangeText={setInstituteName}
                    placeholder="Your college or university"
                    maxLength={200}
                  />
                  {instituteName.length > 0 && !isRegistrationTextValid(instituteName) ? (
                    <Text style={styles.errorText}>Enter a valid college or university name (at least 2 characters).</Text>
                  ) : null}
                  <ActionButton
                    title={supportingDocument?.name || 'Upload supporting document'}
                    secondary
                    onPress={chooseSupportingDocument}
                  />
                  <Text style={styles.passwordHint}>Upload a non-empty affiliation document as PDF, JPG, or PNG (up to 20 MB).</Text>
                </>
              ) : null}
            </>
          ) : null}
          <Field
            label="Email"
            value={email}
            onChangeText={(value) => {
              setEmail(value);
              setLoginEmailError('');
            }}
            placeholder="you@example.com"
            keyboardType="email-address"
            autoCapitalize="none"
            maxLength={isRegistering ? 254 : undefined}
          />
          {!isRegistering && loginEmailError ? (
            <Text style={styles.errorText}>{loginEmailError}</Text>
          ) : null}
          {isRegistering && email.length > 0 && !isEmailValid(email) ? (
            <Text style={styles.errorText}>Enter a valid email address.</Text>
          ) : null}
          <Field
            label="Password"
            value={password}
            onChangeText={(value) => {
              setPassword(value);
              setLoginPasswordError('');
            }}
            placeholder={isRegistering ? 'Create a strong password' : 'Password'}
            secureTextEntry
            showPasswordToggle
            passwordVisible={passwordVisible}
            onTogglePasswordVisibility={() => setPasswordVisible((visible) => !visible)}
          />
          {!isRegistering && loginPasswordError ? (
            <Text style={styles.errorText}>{loginPasswordError}</Text>
          ) : null}
          {isRegistering ? <Text style={styles.passwordHint}>{PASSWORD_REQUIREMENTS}</Text> : null}
          {isRegistering ? (
            <>
              <Field
                label="Confirm password"
                value={confirmPassword}
                onChangeText={setConfirmPassword}
                placeholder="Enter password again"
                secureTextEntry
                showPasswordToggle
                passwordVisible={confirmPasswordVisible}
                onTogglePasswordVisibility={() => setConfirmPasswordVisible((visible) => !visible)}
              />
              {confirmPassword && password !== confirmPassword ? (
                <Text style={styles.errorText}>Passwords do not match.</Text>
              ) : null}
            </>
          ) : null}
          <ActionButton
            title={loading ? (isRegistering ? 'Creating account…' : 'Signing in…') : (isRegistering ? 'Create account' : 'Sign in')}
            onPress={() => isRegistering ? submitRegistration() : submitLogin()}
            disabled={loading || (
              isRegistering && (!email || !password || !isRegistrationTextValid(name) || !isEmailValid(email) || !isPasswordValid(password) || password !== confirmPassword
                || (accountType === 'institute' && (
                  !isRegistrationTextValid(name)
                  || !isEmailValid(email)
                  || !/^\d{10}$/.test(phone)
                  || !isRegistrationTextValid(designation)
                  || !isRegistrationTextValid(instituteName)
                  || !supportingDocument
                )))
            )}
          />
          <Pressable
            accessibilityRole="button"
            disabled={loading}
            onPress={() => setMode(isRegistering ? 'login' : 'register')}
            style={styles.authSwitch}
          >
            <Text style={styles.link}>
              {isRegistering ? 'Already have an account? Sign in' : 'New to Campus-notes? Create an account'}
            </Text>
          </Pressable>
          {!isRegistering ? (
            <Pressable accessibilityRole="button" disabled={loading} onPress={() => setMode('forgot')} style={styles.authSwitch}>
              <Text style={styles.link}>Forgot password?</Text>
            </Pressable>
          ) : null}
        </>
      ) : null}
        </>
      ) : null}
    </View>
  );
}

function InstitutePublishing({ departments, papers, refresh }) {
  const [editing, setEditing] = useState(null);
  const [subject, setSubject] = useState('');
  const [subjectCode, setSubjectCode] = useState('');
  const [departmentId, setDepartmentId] = useState('');
  const [newDepartmentName, setNewDepartmentName] = useState('');
  const [newDepartmentCode, setNewDepartmentCode] = useState('');
  const [semester, setSemester] = useState('1');
  const [examType, setExamType] = useState(EXAM_TYPES[0]);
  const [year, setYear] = useState(String(new Date().getFullYear()));
  const [description, setDescription] = useState('');
  const [file, setFile] = useState(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    if (editing) {
      setSubject(editing.subject);
      setSubjectCode(editing.subjectCode || '');
      setDepartmentId(editing.departmentId);
      setSemester(String(editing.semester));
      setExamType(editing.examType);
      setYear(String(editing.year));
      setDescription(editing.description || '');
      setFile(null);
    } else if (!departmentId && departments.length) {
      setDepartmentId(departments[0].id);
    }
  }, [editing, departments]);

  function clearForm() {
    setEditing(null);
    setSubject('');
    setSubjectCode('');
    setSemester('1');
    setExamType(EXAM_TYPES[0]);
    setYear(String(new Date().getFullYear()));
    setDescription('');
    setFile(null);
  }

  async function choosePdf() {
    try {
      const result = await DocumentPicker.getDocumentAsync({
        type: 'application/pdf',
        copyToCacheDirectory: true,
      });
      if (!result.canceled) setFile(result.assets[0]);
    } catch (error) {
      showThemedAlert('Could not open documents', error.message);
    }
  }

  async function addDepartment() {
    if (!newDepartmentName.trim() || !newDepartmentCode.trim()) {
      showThemedAlert('Missing information', 'Enter the department name and code.');
      return;
    }
    setBusy(true);
    try {
      const created = await request('/api/departments', {
        method: 'POST',
        body: JSON.stringify({ name: newDepartmentName.trim(), code: newDepartmentCode.trim() }),
      });
      setNewDepartmentName('');
      setNewDepartmentCode('');
      setDepartmentId(created.id);
      refresh();
      showThemedAlert('Department added', `${created.name} is now available for question papers.`);
    } catch (error) {
      showThemedAlert('Could not add department', error.message);
    } finally {
      setBusy(false);
    }
  }

  async function savePaper() {
    if (!subject.trim() || !departmentId || (!editing && !file)) {
      showThemedAlert('Missing information', 'Enter the paper details, choose a department, and select a PDF.');
      return;
    }
    setBusy(true);
    try {
      const uploaded = file ? await uploadPdf(file) : null;
      const payload = {
        subject: subject.trim(),
        subject_code: subjectCode.trim() || null,
        department_id: departmentId,
        semester: Number(semester),
        exam_type: examType,
        year,
        description: description.trim() || null,
        ...(uploaded ? { file_url: uploaded.url } : {}),
      };
      await request(editing ? `/api/papers/${editing.id}` : '/api/papers', {
        method: editing ? 'PUT' : 'POST',
        body: JSON.stringify(payload),
      });
      clearForm();
      refresh();
      showThemedAlert('Saved', editing ? 'The paper was updated.' : 'The paper was uploaded.');
    } catch (error) {
      showThemedAlert('Could not save paper', error.message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <>
      <View style={styles.card}>
        <Text style={styles.cardTitle}>{editing ? 'Edit question paper' : 'Upload question paper'}</Text>
        <Field label="Subject" value={subject} onChangeText={setSubject} />
        <Field label="Subject code (optional)" value={subjectCode} onChangeText={setSubjectCode} />
        <ThemedSelect
          label="Department"
          value={departmentId}
          onChange={setDepartmentId}
          options={departments.map((item) => ({ label: item.name, value: item.id }))}
        />
        {!editing ? (
          <>
            <Text style={styles.sectionTitle}>Add a missing department</Text>
            <Field label="Department name" value={newDepartmentName} onChangeText={setNewDepartmentName} maxLength={160} />
            <Field label="Department code" value={newDepartmentCode} onChangeText={setNewDepartmentCode} maxLength={32} autoCapitalize="characters" />
            <ActionButton title="Add department" secondary onPress={addDepartment} disabled={busy || !newDepartmentName.trim() || !newDepartmentCode.trim()} />
          </>
        ) : null}
        <Field label="Semester" value={semester} onChangeText={(value) => setSemester(value.replace(/\D/g, '').slice(0, 2))} keyboardType="number-pad" />
        <ThemedSelect
          label="Exam type"
          value={examType}
          onChange={setExamType}
          options={EXAM_TYPES.map((item) => ({ label: item, value: item }))}
        />
        <Field label="Year" value={year} onChangeText={(value) => setYear(value.replace(/\D/g, '').slice(0, 4))} keyboardType="number-pad" maxLength={4} />
        <Field label="Description (optional)" value={description} onChangeText={setDescription} multiline />
        <ActionButton
          title={file?.name || (editing ? 'Replace PDF (optional)' : 'Choose PDF')}
          secondary
          onPress={choosePdf}
        />
        <View style={styles.actions}>
          <ActionButton
            title={busy ? 'Saving…' : editing ? 'Save changes' : 'Upload paper'}
            onPress={savePaper}
            disabled={busy || !subject.trim() || !departmentId || (!editing && !file)}
          />
          {editing ? <ActionButton title="Cancel" secondary onPress={clearForm} /> : null}
        </View>
      </View>
      <Text style={styles.sectionTitle}>Question papers</Text>
      {papers.map((paper) => (
        <View key={paper.id} style={styles.card}>
          <Text style={styles.cardTitle}>{paper.subject}</Text>
          <Text style={styles.muted}>
            {paper.departmentName || 'Department'} · Semester {paper.semester} · {paper.examType} · {paper.year}
          </Text>
          <ActionButton title="Edit paper" compact secondary onPress={() => setEditing(paper)} />
        </View>
      ))}
      {!papers.length ? <EmptyState text="No question papers are available to edit yet." /> : null}
    </>
  );
}

function EditProfileForm({ user, onUpdated, onCancel }) {
  const [name, setName] = useState(user.name);
  const [instituteName, setInstituteName] = useState(user.instituteName || '');
  const [password, setPassword] = useState('');
  const [passwordVisible, setPasswordVisible] = useState(false);
  const [busy, setBusy] = useState(false);

  async function save() {
    setBusy(true);
    try {
      const updated = await request(`/api/users/${user.id}`, {
        method: 'PUT',
        body: JSON.stringify({
          name: name.trim(),
          ...(user.role === 'institute' ? { institute_name: instituteName.trim() } : {}),
          ...(password ? { password } : {}),
        }),
      });
      setPassword('');
      onUpdated(updated);
      showThemedAlert('Profile updated', 'Your account details have been saved.');
    } catch (error) {
      showThemedAlert('Could not update profile', error.message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <View style={styles.card}>
      <Text style={styles.cardTitle}>Edit profile</Text>
      <Field label="Name" value={name} onChangeText={setName} />
      {user.role === 'institute' ? (
        <Field label="Institute name" value={instituteName} onChangeText={setInstituteName} />
      ) : null}
      <Field
        label="New password (optional)"
        value={password}
        onChangeText={setPassword}
        secureTextEntry
        placeholder="Create a strong password"
        showPasswordToggle
        passwordVisible={passwordVisible}
        onTogglePasswordVisibility={() => setPasswordVisible((visible) => !visible)}
      />
      <Text style={styles.passwordHint}>{PASSWORD_REQUIREMENTS}</Text>
      <ActionButton title={busy ? 'Saving…' : 'Save changes'} onPress={save} disabled={busy || !name.trim() || (user.role === 'institute' && !instituteName.trim()) || (password.length > 0 && !isPasswordValid(password))} />
      <ActionButton title="Cancel" secondary onPress={onCancel} disabled={busy} />
    </View>
  );
}

function AccountScreen({
  user, loading, onLogin, onRegister, onVerifyEmail, onVerifyLogin, onLogout, onAccountRefreshed,
  departments, papers, resources, refresh,
}) {
  const [editingProfile, setEditingProfile] = useState(false);

  async function refreshAccountStatus() {
    try {
      onAccountRefreshed((await request('/api/auth/me')).user);
    } catch (error) {
      showThemedAlert('Could not refresh account status', error.message);
    }
  }

  if (!user) {
    return (
      <ScrollView contentContainerStyle={styles.content} keyboardShouldPersistTaps="handled">
        <Text style={styles.screenTitle}>Your account</Text>
        <LoginPanel
          onLogin={onLogin}
          onRegister={onRegister}
          onVerifyEmail={onVerifyEmail}
          onVerifyLogin={onVerifyLogin}
          loading={loading}
        />
      </ScrollView>
    );
  }

  return (
    <ScrollView contentContainerStyle={styles.content} keyboardShouldPersistTaps="handled">
      <Text style={styles.screenTitle}>Your account</Text>
      <View style={styles.card}>
        <Text style={styles.cardTitle}>{user.name}</Text>
        <Text style={styles.muted}>{user.email}</Text>
        <ActionButton
          title="Edit profile"
          secondary
          onPress={() => setEditingProfile((editing) => !editing)}
        />
        <ActionButton title="Sign out" secondary onPress={onLogout} />
      </View>
      {editingProfile ? (
        <EditProfileForm
          user={user}
          onUpdated={(updated) => {
            onAccountRefreshed(updated);
            setEditingProfile(false);
          }}
          onCancel={() => setEditingProfile(false)}
        />
      ) : null}
      {user.role === 'institute' ? (
        <>
          <View style={styles.card}>
            <Text style={styles.cardTitle}>{user.instituteName}</Text>
            {user.approvalStatus === 'pending' ? (
              <>
                <Text style={styles.muted}>Your institute account is waiting for system admin approval. You can browse papers, but uploading and editing are unavailable until approved.</Text>
                <ActionButton title="Check approval status" secondary onPress={refreshAccountStatus} />
              </>
            ) : user.approvalStatus === 'approved' ? (
              <Text style={styles.muted}>Your institute account is approved. You can upload and edit question papers across departments.</Text>
            ) : (
              <Text style={styles.muted}>Your institute account was not approved. You can still browse papers, but uploading and editing are unavailable.</Text>
            )}
          </View>
          {user.approvalStatus === 'approved' ? (
            <InstitutePublishing departments={departments} papers={papers} refresh={refresh} />
          ) : null}
        </>
      ) : (
        <>
          <View style={styles.card}>
            <Text style={styles.cardTitle}>Your reading account</Text>
            <Text style={styles.muted}>Browse papers and share notes, assignments, and other study materials. Request upload permission once; after approval, future uploads are published automatically.</Text>
            {user.resourceUploadPermissionStatus === 'approved' ? (
              <Text style={styles.badge}>Upload permission approved</Text>
            ) : user.resourceUploadPermissionStatus === 'pending' ? (
              <>
                <Text style={styles.badge}>Upload permission pending</Text>
                <ActionButton title="Refresh permission status" secondary onPress={refreshAccountStatus} />
              </>
            ) : (
              <>
                {user.resourceUploadPermissionStatus === 'rejected' ? <Text style={styles.muted}>Your upload permission request was declined. You may apply again.</Text> : null}
                <ActionButton title="Apply for upload permission" onPress={async () => {
                  try {
                    await request('/api/resources/upload-permission', { method: 'POST' });
                    await refreshAccountStatus();
                    showThemedAlert('Request sent', 'An administrator will review your upload permission request.');
                  } catch (error) {
                    showThemedAlert('Could not send request', error.message);
                  }
                }} />
              </>
            )}
          </View>
          {user.resourceUploadPermissionStatus === 'approved' ? (
            <StudyResourceForm
              departments={departments}
              onSubmitted={refresh}
              onDepartmentAdded={refresh}
            />
          ) : null}
          <View style={styles.rowBetween}>
            <Text style={styles.sectionTitle}>My resource submissions</Text>
            <ActionButton title="Refresh" compact secondary onPress={refresh} />
          </View>
          {resources.filter((resource) => resource.uploadedBy === user.id).map((resource) => (
            <View key={resource.id} style={styles.card}>
              <Text style={styles.cardTitle}>{resource.title}</Text>
              <Text style={styles.muted}>{resource.resourceType} · {resource.departmentName || 'Department'}</Text>
              <Text style={styles.badge}>{resource.status}</Text>
              {resource.status === 'pending' ? <Text style={styles.muted}>Waiting for administrator review. It will be visible in the resource library after approval.</Text> : null}
              {resource.status === 'rejected' ? <Text style={styles.muted}>This submission was not approved. You can remove it and submit a revised file.</Text> : null}
              {resource.status !== 'approved' ? (
                <ActionButton
                  title="Remove submission"
                  secondary
                  onPress={() => showThemedAlert('Remove submission?', 'This permanently removes your unapproved resource.', [
                    { text: 'Cancel', style: 'cancel' },
                    {
                      text: 'Remove',
                      style: 'destructive',
                      onPress: async () => {
                        try {
                          await request(`/api/resources/${resource.id}`, { method: 'DELETE' });
                          refresh();
                        } catch (error) {
                          showThemedAlert('Could not remove submission', error.message);
                        }
                      },
                    },
                  ])}
                />
              ) : null}
            </View>
          ))}
          {!resources.some((resource) => resource.uploadedBy === user.id) ? (
            <EmptyState text="You have not submitted any study resources yet." />
          ) : null}
        </>
      )}
    </ScrollView>
  );
}

function StudyResourceForm({ departments, onSubmitted, onDepartmentAdded }) {
  const [title, setTitle] = useState('');
  const [resourceType, setResourceType] = useState('notes');
  const [departmentId, setDepartmentId] = useState('');
  const [addingDepartment, setAddingDepartment] = useState(false);
  const [newDepartmentName, setNewDepartmentName] = useState('');
  const [newDepartmentCode, setNewDepartmentCode] = useState('');
  const [subject, setSubject] = useState('');
  const [semester, setSemester] = useState('');
  const [academicYear, setAcademicYear] = useState('');
  const [description, setDescription] = useState('');
  const [file, setFile] = useState(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    if (!departmentId && departments.length) setDepartmentId(departments[0].id);
  }, [departmentId, departments]);

  async function choosePdf() {
    try {
      const result = await DocumentPicker.getDocumentAsync({
        type: 'application/pdf',
        copyToCacheDirectory: true,
      });
      if (!result.canceled) setFile(result.assets[0]);
    } catch (error) {
      showThemedAlert('Could not open documents', error.message);
    }
  }

  async function addDepartment() {
    if (!newDepartmentName.trim() || !newDepartmentCode.trim()) {
      showThemedAlert('Missing information', 'Enter the department name and code.');
      return;
    }
    setBusy(true);
    try {
      const created = await request('/api/departments', {
        method: 'POST',
        body: JSON.stringify({
          name: newDepartmentName.trim(),
          code: newDepartmentCode.trim(),
        }),
      });
      setDepartmentId(created.id);
      setNewDepartmentName('');
      setNewDepartmentCode('');
      setAddingDepartment(false);
      onDepartmentAdded();
      showThemedAlert('Department added', `${created.name} is now available for study resources.`);
    } catch (error) {
      showThemedAlert('Could not add department', error.message);
    } finally {
      setBusy(false);
    }
  }

  async function submit() {
    if (!title.trim() || !departmentId || !file) {
      showThemedAlert('Missing information', 'Enter a title, choose a department, and select a PDF.');
      return;
    }
    setBusy(true);
    try {
      const resource = await uploadStudyResource(file, {
        title: title.trim(),
        resourceType,
        departmentId,
        subject: subject.trim(),
        semester,
        academicYear: academicYear.trim(),
        description: description.trim(),
      });
      setTitle('');
      setSubject('');
      setSemester('');
      setAcademicYear('');
      setDescription('');
      setFile(null);
      onSubmitted();
      showThemedAlert(
        resource.status === 'approved' ? 'Resource published' : 'Submitted for review',
        resource.status === 'approved'
          ? 'Your resource is published and available in the library.'
          : 'An administrator must approve your resource before readers can access it.',
      );
    } catch (error) {
      showThemedAlert('Could not submit resource', error.message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <View style={styles.card}>
      <Text style={styles.cardTitle}>Share a study resource</Text>
      <Field label="Title" value={title} onChangeText={setTitle} maxLength={200} />
      <ThemedSelect
        label="Resource type"
        value={resourceType}
        onChange={setResourceType}
        options={[
          { label: 'Notes', value: 'notes' },
          { label: 'Assignments', value: 'assignments' },
          { label: 'Other study material', value: 'other' },
        ]}
      />
      <ThemedSelect
        label="Department"
        value={departmentId}
        onChange={setDepartmentId}
        options={departments.map((item) => ({ label: `${item.name} (${item.code})`, value: item.id }))}
      />
      <Pressable
        accessibilityRole="button"
        onPress={() => setAddingDepartment((visible) => !visible)}
        style={styles.addDepartmentToggle}
      >
        <Text style={styles.link}>{addingDepartment ? 'Cancel adding department' : 'Can’t find your department? Add it'}</Text>
      </Pressable>
      {addingDepartment ? (
        <View style={styles.departmentForm}>
          <Field label="Department name" value={newDepartmentName} onChangeText={setNewDepartmentName} maxLength={160} />
          <Field label="Department code" value={newDepartmentCode} onChangeText={setNewDepartmentCode} maxLength={32} autoCapitalize="characters" />
          <ActionButton
            title={busy ? 'Adding…' : 'Add department'}
            secondary
            onPress={addDepartment}
            disabled={busy || !newDepartmentName.trim() || !newDepartmentCode.trim()}
          />
        </View>
      ) : null}
      <Field label="Subject (optional)" value={subject} onChangeText={setSubject} maxLength={200} />
      <Field label="Semester (optional)" value={semester} onChangeText={(value) => setSemester(value.replace(/\D/g, '').slice(0, 2))} keyboardType="number-pad" />
      <Field label="Academic year (optional)" value={academicYear} onChangeText={setAcademicYear} placeholder="2026 or 2026-27" maxLength={8} />
      <Field label="Description (optional)" value={description} onChangeText={setDescription} multiline maxLength={2000} />
      <Text style={styles.muted}>PDF files only, up to the platform upload limit. Uploads are available after administrator permission is granted.</Text>
      <ActionButton title={file?.name || 'Choose PDF'} secondary onPress={choosePdf} />
      <ActionButton title={busy ? 'Uploading…' : 'Upload resource'} onPress={submit} disabled={busy || !title.trim() || !departmentId || !file} />
    </View>
  );
}

function ResourcesScreen({ user, resources, onSignIn, onView, onDownload, onShare }) {
  const approvedResources = resources.filter((resource) => resource.status === 'approved');
  return (
    <ScrollView contentContainerStyle={styles.content}>
      <Text style={styles.screenTitle}>Study resources</Text>
      <Text style={styles.muted}>Browse notes, assignments, and other study materials approved by administrators.</Text>
      {!user ? (
        <View style={styles.card}>
          <Text style={styles.muted}>Browse approved resources below. Sign in to open or download files.</Text>
          <ActionButton title="Sign in" onPress={onSignIn} />
        </View>
      ) : null}
      {approvedResources.map((resource) => (
        <View key={resource.id} style={styles.card}>
          <Text style={styles.cardTitle}>{resource.title}</Text>
          <Text style={styles.muted}>
            {resource.resourceType} · {resource.departmentName || 'Department'}
            {resource.subject ? ` · ${resource.subject}` : ''}
            {resource.semester ? ` · Semester ${resource.semester}` : ''}
            {resource.academicYear ? ` · ${resource.academicYear}` : ''}
          </Text>
          {resource.description ? <Text style={styles.description}>{resource.description}</Text> : null}
          <View style={styles.actions}>
            <ActionButton title="View PDF" compact onPress={() => onView(resource)} />
            <ActionButton title="Download" compact secondary onPress={() => onDownload(resource)} />
            <ActionButton title="Share" compact secondary onPress={() => onShare(resource)} />
          </View>
        </View>
      ))}
      {!approvedResources.length ? <EmptyState text="No approved study resources are available yet." /> : null}
    </ScrollView>
  );
}

function EmptyState({ text }) {
  return <View style={styles.empty}><Text style={styles.muted}>{text}</Text></View>;
}

async function openPaper(paper, refresh) {
  try {
    if (!paper.fileUrl) {
      showThemedAlert('PDF unavailable', 'This paper does not have a PDF attached.');
      return;
    }
    await openCachedPdf(await cachePdf(paper.fileUrl, `${paper.subject}-${paper.year}`));
    // The view counter is analytics: it must never decide whether a student can
    // read a paper, so it runs after the PDF opens and never blocks the flow.
    request(`/api/papers/${paper.id}/views`, { method: 'POST' })
      .then(() => refresh())
      .catch(() => {});
  } catch (error) {
    showThemedAlert('Could not open paper', error.message);
  }
}

async function openCachedPdf(uri) {
  if (Platform.OS === 'android') {
    const contentUri = await FileSystem.getContentUriAsync(uri);
    await IntentLauncher.startActivityAsync('android.intent.action.VIEW', {
      data: contentUri,
      type: 'application/pdf',
      flags: 1,
    });
    return;
  }
  await Linking.openURL(uri);
}

function safePdfBaseName(value) {
  return (value || 'study-resource')
    .replace(/\.pdf$/i, '')
    .replace(/[^A-Za-z0-9._-]/g, '_')
    .slice(0, 90) || 'study-resource';
}

async function cachePdf(fileUrl, fileName, resource) {
  if (!FileSystem.cacheDirectory) throw new Error('The device does not have a cache folder available.');
  const localUri = `${FileSystem.cacheDirectory}${safePdfBaseName(fileName)}-${Date.now()}.pdf`;
  const result = resource
    ? await downloadStudyResource(resource, localUri)
    : await downloadPaperFile(fileUrl, localUri);
  if (result.status < 200 || result.status >= 300) {
    throw new Error(`The server returned status ${result.status}.`);
  }
  return result.uri;
}

async function savePdfToDevice(fileUrl, fileName, resource) {
  const cachedUri = await cachePdf(fileUrl, fileName, resource);
  const name = `${safePdfBaseName(fileName)}-${Date.now()}`;
  if (Platform.OS === 'android') {
    const storage = FileSystem.StorageAccessFramework;
    if (!storage?.requestDirectoryPermissionsAsync) {
      throw new Error('Choose a download folder is not available on this Android version.');
    }
    const initialDownloads = storage.getUriForDirectoryInRoot('Download');
    const permission = await storage.requestDirectoryPermissionsAsync(initialDownloads);
    if (!permission.granted) {
      await FileSystem.deleteAsync(cachedUri, { idempotent: true });
      return null;
    }
    const destination = await storage.createFileAsync(permission.directoryUri, name, 'application/pdf');
    const contents = await FileSystem.readAsStringAsync(cachedUri, {
      encoding: FileSystem.EncodingType.Base64,
    });
    await storage.writeAsStringAsync(destination, contents, {
      encoding: FileSystem.EncodingType.Base64,
    });
    await FileSystem.deleteAsync(cachedUri, { idempotent: true });
    return 'the folder you selected';
  }

  if (!FileSystem.documentDirectory) throw new Error('The device does not have a documents folder available.');
  const destination = `${FileSystem.documentDirectory}${name}.pdf`;
  await FileSystem.copyAsync({ from: cachedUri, to: destination });
  await FileSystem.deleteAsync(cachedUri, { idempotent: true });
  return 'Campus Notes app storage';
}

async function viewResource(resource) {
  try {
    await openCachedPdf(await cachePdf(resource.fileUrl, resource.title, resource));
  } catch (error) {
    showThemedAlert('Could not open PDF', error.message);
  }
}

async function sharePdf(fileUrl, fileName, resource) {
  try {
    const uri = await cachePdf(fileUrl, fileName, resource);
    if (!(await Sharing.isAvailableAsync())) throw new Error('Sharing is not available on this device.');
    await Sharing.shareAsync(uri, {
      mimeType: 'application/pdf',
      dialogTitle: `Share ${fileName}`,
      UTI: 'com.adobe.pdf',
    });
  } catch (error) {
    showThemedAlert('Could not share PDF', error.message);
  }
}

async function downloadPaper(paper, refresh) {
  try {
    if (!paper.fileUrl) {
      showThemedAlert('PDF unavailable', 'This paper does not have a PDF attached.');
      return;
    }
    const savedLocation = await savePdfToDevice(paper.fileUrl, `${paper.subject}-${paper.year}`);
    if (!savedLocation) return;

    request(`/api/papers/${paper.id}/downloads`, { method: 'POST' })
      .then(() => refresh())
      .catch(() => {});
    showThemedAlert('Download complete', `Saved ${paper.subject} to ${savedLocation}.`);
  } catch (error) {
    showThemedAlert('Could not download paper', error.message);
  }
}

async function downloadResource(resource) {
  try {
    const savedLocation = await savePdfToDevice(resource.fileUrl, resource.title, resource);
    if (!savedLocation) return;
    showThemedAlert('Download complete', `Saved ${resource.title} to ${savedLocation}.`);
  } catch (error) {
    showThemedAlert('Could not download resource', error.message);
  }
}

export default function App() {
  const { width } = useWindowDimensions();
  const compact = width < 360;
  const [tab, setTab] = useState('Home');
  const [user, setUser] = useState(null);
  const [papers, setPapers] = useState([]);
  const [resources, setResources] = useState([]);
  const [departments, setDepartments] = useState([]);
  const [favorites, setFavorites] = useState([]);
  const [loading, setLoading] = useState(false);
  const [notice, setNotice] = useState('');
  const [dialog, setDialog] = useState(null);
  const [refreshVersion, setRefreshVersion] = useState(0);
  const [browseDepartment, setBrowseDepartment] = useState('');

  const refresh = useCallback(() => setRefreshVersion((value) => value + 1), []);
  const showDialog = useCallback((title, message, buttons) => {
    setDialog({ title, message, buttons: buttons?.length ? buttons : [{ text: 'OK' }] });
  }, []);

  useEffect(() => {
    themedAlertDispatcher = showDialog;
    return () => {
      if (themedAlertDispatcher === showDialog) themedAlertDispatcher = null;
    };
  }, [showDialog]);

  useEffect(() => {
    getStoredUser()
      .then((storedUser) => setUser(storedUser))
      .catch((error) => setNotice(
        error.status === 401 || error.status === 403
          ? 'Your saved session has expired. Please sign in again.'
          : `Could not verify saved session: ${error.message}`,
      ));
  }, []);

  useEffect(() => {
    let active = true;
    Promise.all([request('/api/papers'), request('/api/departments')])
      .then(([nextPapers, nextDepartments]) => {
        if (active) {
          setPapers(nextPapers);
          setDepartments(nextDepartments);
          setNotice('');
        }
      })
      .catch((error) => {
        if (active) setNotice(`Could not load Campus-notes data: ${error.message}`);
      });
    return () => { active = false; };
  }, [refreshVersion]);

  useEffect(() => {
    let active = true;
    request('/api/resources')
      .then((items) => { if (active) setResources(items); })
      .catch((error) => {
        if (active) setNotice(`Could not load study resources: ${error.message}`);
      });
    return () => { active = false; };
  }, [user, refreshVersion]);

  useEffect(() => {
    let active = true;
    if (user) {
      request('/api/favorites')
        .then((items) => { if (active) setFavorites(items); })
        .catch((error) => { if (active) setNotice(`Could not load favorites: ${error.message}`); });
    } else {
      setFavorites([]);
    }
    return () => { active = false; };
  }, [user, refreshVersion]);

  async function handleLogin(email, password, expectedAccountType) {
    setLoading(true);
    try {
      const result = await signIn(email, password, expectedAccountType);
      setNotice('');
      return result;
    } catch (error) {
      setNotice('');
      throw error;
    } finally {
      setLoading(false);
    }
  }

  async function handleLoginOtp(email, otp, expectedAccountType) {
    setLoading(true);
    try {
      const loggedInUser = await confirmLoginOtp(email, otp, expectedAccountType);
      setUser(loggedInUser);
      setNotice('');
      setTab('Account');
      refresh();
    } finally {
      setLoading(false);
    }
  }

  async function handleRegister(name, email, password, accountType, instituteName, instituteDetails) {
    setLoading(true);
    try {
      return await register(
        name,
        email,
        password,
        accountType,
        instituteName,
        instituteDetails,
      );
    } catch (error) {
      setNotice('');
      throw error;
    } finally {
      setLoading(false);
    }
  }

  async function handleEmailVerification(email, otp) {
    const verifiedUser = await confirmEmailVerification(email, otp);
    setUser(verifiedUser);
    setNotice(verifiedUser.role === 'institute' && verifiedUser.approvalStatus !== 'approved'
      ? 'Your email is verified. Institute features remain locked until an administrator approves your account.'
      : '');
    setTab('Account');
    refresh();
  }

  function runProtectedFeature(feature, callback) {
    if (user) return callback();
    showDialog(
      'Sign in to continue',
      `Create an account or sign in to ${feature}.`,
      [
        { text: 'Not now', style: 'cancel' },
        { text: 'Sign up or sign in', onPress: () => setTab('Account') },
      ],
    );
  }

  function handleLogout() {
    showThemedAlert('Sign out?', 'Are you sure you want to sign out?', [
      { text: 'Cancel', style: 'cancel' },
      {
        text: 'Sign out',
        style: 'destructive',
        onPress: async () => {
          // Signing out must always succeed locally: the stored token is deleted
          // either way, so leaving the UI signed in would only show a session
          // that can no longer make any authenticated request.
          try {
            await signOut();
          } catch (error) {
            showThemedAlert(
              'Signed out on this device',
              'The session was removed here, but the server could not be told. Sign in again to refresh it.',
            );
          } finally {
            setUser(null);
            setFavorites([]);
            setTab('Home');
          }
        },
      },
    ]);
  }

  async function toggleFavorite(paperId) {
    if (!user) {
      runProtectedFeature('save favorite papers', () => {});
      return;
    }
    try {
      const result = await request(`/api/favorites/${paperId}`, { method: 'POST' });
      setFavorites((current) => result.favorited
        ? [...current, paperId]
        : current.filter((id) => id !== paperId));
    } catch (error) {
      setNotice(`Could not update favorite: ${error.message}`);
    }
  }

  let screen;
  if (tab === 'Resources') {
    screen = (
      <ResourcesScreen
        user={user}
        resources={resources}
        onSignIn={() => setTab('Account')}
        onView={(resource) => runProtectedFeature('open study resources', () => viewResource(resource))}
        onDownload={(resource) => runProtectedFeature('download study resources', () => downloadResource(resource))}
        onShare={(resource) => runProtectedFeature('share study resources', () => sharePdf(resource.fileUrl, resource.title, resource))}
      />
    );
  } else if (tab === 'AI Preparation') {
    screen = (
      <AIPreparationScreen
        user={user}
        onBack={() => setTab('Home')}
        onSignIn={() => setTab('Account')}
      />
    );
  } else if (tab === 'Home') {
    screen = (
      <HomeScreen
        papers={papers}
        departments={departments}
        user={user}
        favorites={favorites}
        onFavorite={toggleFavorite}
        onBrowse={(deptId) => {
          if (deptId) setBrowseDepartment(deptId);
          setTab('Browse');
        }}
        onOpenPaper={(paper) => runProtectedFeature('open question papers', () => openPaper(paper, refresh))}
        onDownloadPaper={(paper) => runProtectedFeature('download question papers', () => downloadPaper(paper, refresh))}
        onSharePaper={(paper) => runProtectedFeature('share question papers', () => sharePdf(paper.fileUrl, `${paper.subject}-${paper.year}`))}
        onAIPreparation={() => runProtectedFeature('use AI exam preparation', () => setTab('AI Preparation'))}
        compact={compact}
      />
    );
  } else if (tab === 'Browse') {
    screen = (
      <BrowseScreen
        papers={papers}
        departments={departments}
        user={user}
        favorites={favorites}
        initialDepartment={browseDepartment}
        onFavorite={toggleFavorite}
        onOpenPaper={(paper) => runProtectedFeature('open question papers', () => openPaper(paper, refresh))}
        onDownloadPaper={(paper) => runProtectedFeature('download question papers', () => downloadPaper(paper, refresh))}
        onSharePaper={(paper) => runProtectedFeature('share question papers', () => sharePdf(paper.fileUrl, `${paper.subject}-${paper.year}`))}
        refresh={refresh}
        clearNotice={() => setNotice('')}
        compact={compact}
      />
    );
  } else if (tab === 'Departments') {
    screen = (
      <DepartmentsScreen
        departments={departments}
        papers={papers}
        onBrowse={(departmentId) => {
          setBrowseDepartment(departmentId);
          setTab('Browse');
        }}
      />
    );
  } else {
    screen = (
      <AccountScreen
        user={user}
        loading={loading}
        onLogin={handleLogin}
        onRegister={handleRegister}
        onVerifyEmail={handleEmailVerification}
        onVerifyLogin={handleLoginOtp}
        onLogout={handleLogout}
        onAccountRefreshed={setUser}
        departments={departments}
        papers={papers}
        resources={resources}
        refresh={refresh}
      />
    );
  }

  return (
    <SafeAreaProvider>
      <SafeAreaView style={styles.safe}>
        <View style={[styles.header, compact && styles.headerCompact]}>
          <View style={styles.brandRow}>
            <Image source={require('./notes-logo.png')} style={styles.logo} resizeMode="contain" />
            <View style={styles.brandInfo}>
              <Text style={styles.brand}>Campus-notes</Text>
              <Text style={styles.headerCaption}>Question paper library</Text>
            </View>
          </View>
        </View>
        <View style={styles.noticeContainer}>
          <Notice message={notice} onDismiss={() => setNotice('')} />
        </View>
        <View style={styles.body}>{screen}</View>
        <View style={styles.tabs}>
          {['Home', 'Browse', 'Resources', 'Departments', 'Account'].map((item) => (
            <Pressable key={item} style={styles.tab} onPress={() => setTab(item)} accessibilityRole="tab" accessibilityState={{ selected: tab === item }}>
              <Text numberOfLines={1} style={[styles.tabText, compact && styles.tabTextCompact, tab === item && styles.activeTabText]}>{item}</Text>
              {tab === item ? <View style={styles.activeTabIndicator} /> : null}
            </Pressable>
          ))}
        </View>
      </SafeAreaView>
      <Modal
        visible={Boolean(dialog)}
        transparent
        animationType="fade"
        onRequestClose={() => setDialog(null)}
      >
        <View style={styles.dialogBackdrop}>
          <View style={styles.dialogCard} accessibilityRole="alert">
            <Text style={styles.dialogTitle}>{dialog?.title}</Text>
            {dialog?.message ? <Text style={styles.dialogMessage}>{dialog.message}</Text> : null}
            <View style={styles.dialogActions}>
              {(dialog?.buttons || []).map((button, index) => (
                <Pressable
                  key={`${button.text}-${index}`}
                  accessibilityRole="button"
                  style={[
                    styles.dialogButton,
                    button.style === 'cancel' && styles.dialogCancelButton,
                    button.style === 'destructive' && styles.dialogDestructiveButton,
                  ]}
                  onPress={() => {
                    setDialog(null);
                    button.onPress?.();
                  }}
                >
                  <Text style={[
                    styles.dialogButtonText,
                    button.style === 'cancel' && styles.dialogCancelText,
                  ]}>{button.text}</Text>
                </Pressable>
              ))}
            </View>
          </View>
        </View>
      </Modal>
    </SafeAreaProvider>
  );
}

const styles = StyleSheet.create({
  safe: { flex: 1, backgroundColor: COLORS.deepNavy },
  header: {
    paddingHorizontal: 16,
    paddingVertical: 12,
    backgroundColor: 'rgba(16, 42, 67, 0.96)',
    borderBottomWidth: 1,
    borderBottomColor: COLORS.glassBorder,
  },
  headerCompact: { paddingHorizontal: 12, paddingVertical: 10 },
  brandRow: { flexDirection: 'row', alignItems: 'center', gap: 10 },
  logo: { width: 38, height: 38, borderRadius: 10 },
  brandInfo: { flex: 1 },
  brand: { fontSize: 22, fontWeight: '800', color: COLORS.white },
  headerCaption: { color: COLORS.softGrey, marginTop: 2, fontSize: 13 },
  body: { flex: 1 },
  content: {
    width: '100%',
    maxWidth: 760,
    alignSelf: 'center',
    paddingHorizontal: 14,
    paddingTop: 14,
    paddingBottom: 28,
    gap: 12,
  },

  // Home Ambient Background Orbs (Glassmorphism Gradient Backing)
  homeContainer: { flex: 1, position: 'relative', overflow: 'hidden' },
  aiPreparationHomeCard: {
    borderRadius: 18,
    borderWidth: 1,
    borderColor: COLORS.glassBorderActive,
    backgroundColor: COLORS.cardSurface,
    padding: 16,
    marginTop: 14,
    gap: 6,
  },
  aiPreparationEyebrow: { color: COLORS.mintGreen, fontSize: 11, fontWeight: '800', letterSpacing: 1 },
  aiPreparationTitle: { color: COLORS.white, fontSize: 18, fontWeight: '800' },
  aiPreparationCopy: { color: COLORS.coolGrey, fontSize: 13, lineHeight: 19 },
  aiPreparationLink: { color: COLORS.mintGreen, fontSize: 14, fontWeight: '800', marginTop: 3 },
  homeScrollContent: {
    width: '100%',
    maxWidth: 760,
    alignSelf: 'center',
    paddingHorizontal: 14,
    paddingTop: 14,
    paddingBottom: 32,
    gap: 14,
  },
  // Welcome Hero Glass Card
  heroGlass: {
    backgroundColor: COLORS.glassBackground,
    borderRadius: 20,
    padding: 20,
    gap: 12,
    borderWidth: 1,
    borderColor: COLORS.glassBorder,
    shadowColor: '#000',
    shadowOffset: { width: 0, height: 8 },
    shadowOpacity: 0.25,
    shadowRadius: 16,
    elevation: 5,
  },
  hero: {
    backgroundColor: COLORS.glassBackground,
    borderRadius: 20,
    padding: 20,
    gap: 12,
    borderWidth: 1,
    borderColor: COLORS.glassBorder,
  },
  heroCompact: { padding: 16, gap: 10 },
  eyebrowContainer: {
    flexDirection: 'row',
    alignItems: 'center',
    gap: 7,
    alignSelf: 'flex-start',
    backgroundColor: 'rgba(97, 214, 179, 0.14)',
    paddingHorizontal: 10,
    paddingVertical: 5,
    borderRadius: 12,
    borderWidth: 1,
    borderColor: 'rgba(97, 214, 179, 0.35)',
  },
  eyebrow: { color: COLORS.mintGreen, fontWeight: '800', fontSize: 11, letterSpacing: 1.5 },
  eyebrowDot: { width: 4, height: 4, borderRadius: 2, backgroundColor: COLORS.mintGreen },
  eyebrowLocation: { color: COLORS.coolGrey, fontWeight: '700', fontSize: 11, letterSpacing: 1 },
  heroTitle: { color: COLORS.white, fontWeight: '800', fontSize: 26, lineHeight: 33 },
  heroTitleCompact: { fontSize: 22, lineHeight: 28 },
  heroCopy: { color: COLORS.coolGrey, fontSize: 14, lineHeight: 21 },
  heroActionRow: { marginTop: 4 },

  // Home Search Bar
  homeSearchContainer: {
    flexDirection: 'row',
    alignItems: 'center',
    backgroundColor: COLORS.glassInput,
    borderWidth: 1,
    borderColor: COLORS.glassBorder,
    borderRadius: 14,
    paddingHorizontal: 14,
    paddingVertical: 4,
  },
  searchIcon: { fontSize: 16, marginRight: 8 },
  homeSearchInput: { flex: 1, fontSize: 15, color: COLORS.white, minHeight: 44 },
  searchClearBtn: { padding: 6 },
  searchClearText: { color: COLORS.softGrey, fontSize: 14, fontWeight: '700' },

  // Statistics Deck
  statsRow: { flexDirection: 'row', gap: 10 },
  statCard: {
    flex: 1,
    backgroundColor: COLORS.glassBackground,
    borderWidth: 1,
    borderColor: COLORS.glassBorder,
    borderRadius: 16,
    paddingVertical: 14,
    paddingHorizontal: 10,
    alignItems: 'center',
    shadowColor: '#000',
    shadowOffset: { width: 0, height: 4 },
    shadowOpacity: 0.18,
    shadowRadius: 8,
    elevation: 3,
  },
  stat: {
    flex: 1,
    minWidth: 82,
    padding: 12,
    borderRadius: 14,
    backgroundColor: COLORS.glassBackground,
    borderWidth: 1,
    borderColor: COLORS.glassBorder,
    alignItems: 'center',
  },
  statValue: { color: COLORS.mintGreen, fontSize: 22, fontWeight: '800' },
  statLabel: { color: COLORS.softGrey, fontSize: 12, fontWeight: '600', marginTop: 3 },

  // Department Filter Chips
  deptSection: { gap: 10, marginTop: 2 },
  sectionHeaderRow: {
    flexDirection: 'row',
    justifyContent: 'space-between',
    alignItems: 'center',
    marginTop: 4,
  },
  sectionTitle: { color: COLORS.white, fontSize: 18, fontWeight: '800', letterSpacing: 0.3 },
  clearFilterLink: { color: COLORS.mintGreen, fontSize: 13, fontWeight: '700' },
  browseAllLink: { color: COLORS.mintGreen, fontSize: 14, fontWeight: '700' },
  deptScroll: { gap: 8, paddingVertical: 2 },
  glassChip: {
    backgroundColor: 'rgba(255, 255, 255, 0.07)',
    borderWidth: 1,
    borderColor: COLORS.glassBorder,
    paddingHorizontal: 14,
    paddingVertical: 8,
    borderRadius: 20,
  },
  activeGlassChip: {
    backgroundColor: COLORS.mintGreen,
    borderColor: COLORS.mintGreen,
  },
  glassChipText: { color: COLORS.coolGrey, fontSize: 13, fontWeight: '600' },
  activeGlassChipText: { color: COLORS.deepNavy, fontWeight: '800' },

  // Paper Cards (Glassmorphism & High Readability)
  paperGlassCard: {
    backgroundColor: COLORS.cardSurface,
    borderWidth: 1,
    borderColor: COLORS.glassBorder,
    borderRadius: 18,
    padding: 16,
    gap: 10,
    shadowColor: '#000',
    shadowOffset: { width: 0, height: 6 },
    shadowOpacity: 0.22,
    shadowRadius: 12,
    elevation: 4,
  },
  paperTopRow: {
    flexDirection: 'row',
    justifyContent: 'space-between',
    alignItems: 'flex-start',
    gap: 8,
  },
  paperTitle: { fontSize: 17, fontWeight: '700', color: COLORS.white, lineHeight: 23 },
  favButton: { padding: 2 },
  favoriteMint: { color: COLORS.mintGreen, fontSize: 24, lineHeight: 26 },
  pillRow: { flexDirection: 'row', flexWrap: 'wrap', gap: 6, alignItems: 'center' },
  deptBadge: {
    backgroundColor: 'rgba(22, 125, 120, 0.28)',
    borderWidth: 1,
    borderColor: 'rgba(97, 214, 179, 0.35)',
    paddingHorizontal: 9,
    paddingVertical: 4,
    borderRadius: 12,
  },
  deptBadgeText: { color: COLORS.mintGreen, fontSize: 12, fontWeight: '700' },
  metaPill: {
    backgroundColor: 'rgba(255, 255, 255, 0.08)',
    paddingHorizontal: 9,
    paddingVertical: 4,
    borderRadius: 12,
  },
  metaPillText: { color: COLORS.coolGrey, fontSize: 12, fontWeight: '600' },
  subjectCodeText: { color: COLORS.softGrey, fontSize: 13 },
  paperDescription: { color: COLORS.coolGrey, fontSize: 13, lineHeight: 19 },
  paperMetricsRow: { flexDirection: 'row', alignItems: 'center', gap: 6 },
  metricText: { color: COLORS.softGrey, fontSize: 12 },
  metricDivider: { color: COLORS.darkMuted, fontSize: 10 },

  // Empty State
  emptyGlass: {
    backgroundColor: COLORS.glassBackground,
    borderWidth: 1,
    borderColor: COLORS.glassBorder,
    borderRadius: 18,
    padding: 24,
    alignItems: 'center',
    gap: 10,
  },
  emptyIcon: { fontSize: 34 },
  emptyTitle: { color: COLORS.white, fontSize: 17, fontWeight: '700' },
  emptySub: { color: COLORS.softGrey, fontSize: 14, textAlign: 'center' },

  // Generic Card & Inputs
  card: {
    width: '100%',
    backgroundColor: COLORS.cardSurface,
    borderRadius: 18,
    padding: 16,
    borderWidth: 1,
    borderColor: COLORS.glassBorder,
    gap: 8,
  },
  rowBetween: { flexDirection: 'row', justifyContent: 'space-between', alignItems: 'center', gap: 8 },
  cardTitle: { fontSize: 17, fontWeight: '700', color: COLORS.white, flexShrink: 1 },
  muted: { color: COLORS.softGrey, fontSize: 14, lineHeight: 20 },
  description: { color: COLORS.coolGrey, lineHeight: 20 },
  label: { color: COLORS.offWhite, fontWeight: '600', marginBottom: 5 },
  field: { width: '100%', minWidth: 0, marginTop: 3 },
  input: {
    width: '100%',
    minWidth: 0,
    borderWidth: 1,
    borderColor: COLORS.glassBorder,
    backgroundColor: COLORS.glassInput,
    borderRadius: 12,
    paddingHorizontal: 12,
    paddingVertical: 10,
    fontSize: 15,
    color: COLORS.white,
  },
  passwordInputRow: {
    width: '100%',
    minWidth: 0,
    flexDirection: 'row',
    alignItems: 'center',
    borderWidth: 1,
    borderColor: COLORS.glassBorder,
    backgroundColor: COLORS.glassInput,
    borderRadius: 12,
  },
  passwordInput: { flex: 1, width: 'auto', borderWidth: 0, backgroundColor: 'transparent', color: COLORS.white },
  passwordToggle: { minHeight: 42, justifyContent: 'center', paddingHorizontal: 12 },
  passwordToggleText: { color: COLORS.mintGreen, fontWeight: '700' },
  passwordHint: { color: COLORS.softGrey, fontSize: 13, lineHeight: 18 },
  multiline: { minHeight: 76, textAlignVertical: 'top' },
  picker: {
    width: '100%',
    minWidth: 0,
    borderWidth: 1,
    borderColor: COLORS.glassBorder,
    borderRadius: 12,
    backgroundColor: COLORS.glassInput,
    overflow: 'hidden',
  },
  selectButton: {
    width: '100%',
    minHeight: 48,
    flexDirection: 'row',
    alignItems: 'center',
    justifyContent: 'space-between',
    borderWidth: 1,
    borderColor: COLORS.glassBorder,
    borderRadius: 12,
    backgroundColor: COLORS.glassInput,
    paddingHorizontal: 14,
    paddingVertical: 11,
  },
  selectButtonText: { color: COLORS.white, fontSize: 15, flex: 1 },
  selectPlaceholder: { color: COLORS.softGrey, fontSize: 15, flex: 1 },
  selectChevron: { color: COLORS.mintGreen, fontSize: 19, fontWeight: '800', marginLeft: 10 },
  selectBackdrop: {
    flex: 1,
    justifyContent: 'center',
    alignItems: 'center',
    padding: 24,
    backgroundColor: 'rgba(4, 15, 24, 0.72)',
  },
  selectDialog: {
    width: '100%',
    maxWidth: 440,
    maxHeight: '80%',
    padding: 18,
    gap: 10,
    borderRadius: 18,
    borderWidth: 1,
    borderColor: COLORS.glassBorder,
    backgroundColor: COLORS.deepNavy,
  },
  selectOptions: { flexShrink: 1 },
  selectOption: {
    minHeight: 48,
    flexDirection: 'row',
    alignItems: 'center',
    justifyContent: 'space-between',
    paddingHorizontal: 12,
    borderBottomWidth: 1,
    borderBottomColor: COLORS.glassBorder,
  },
  selectOptionActive: {
    borderRadius: 10,
    backgroundColor: 'rgba(97, 214, 179, 0.14)',
  },
  selectOptionText: { color: COLORS.coolGrey, fontSize: 15, flex: 1 },
  selectOptionTextActive: { color: COLORS.mintGreen, fontWeight: '700' },
  selectCheck: { color: COLORS.mintGreen, fontSize: 17, fontWeight: '800', marginLeft: 10 },
  addDepartmentToggle: { alignSelf: 'flex-start', paddingVertical: 5 },
  departmentForm: {
    gap: 8,
    padding: 12,
    borderWidth: 1,
    borderColor: COLORS.glassBorder,
    borderRadius: 14,
    backgroundColor: COLORS.glassBackground,
  },
  button: {
    backgroundColor: COLORS.forestGreen,
    borderRadius: 12,
    minHeight: 44,
    paddingHorizontal: 16,
    paddingVertical: 11,
    alignItems: 'center',
    justifyContent: 'center',
    marginTop: 5,
  },
  buttonText: { color: COLORS.white, fontWeight: '700', fontSize: 15 },
  buttonPressed: { opacity: 0.72, transform: [{ scale: 0.95 }] },
  secondaryButton: {
    backgroundColor: 'rgba(255, 255, 255, 0.08)',
    borderWidth: 1,
    borderColor: 'rgba(255, 255, 255, 0.2)',
  },
  secondaryButtonText: { color: COLORS.offWhite },
  compactButton: { minHeight: 36, paddingVertical: 8, paddingHorizontal: 12 },
  disabledButton: { opacity: 0.5 },
  actions: { flexDirection: 'row', gap: 8, alignItems: 'center', flexWrap: 'wrap' },
  favorite: { color: COLORS.mintGreen, fontSize: 26 },
  link: { color: COLORS.mintGreen, fontWeight: '700' },
  authSwitch: { alignItems: 'center', paddingVertical: 8 },
  errorText: { color: COLORS.red, fontSize: 13 },
  badge: {
    alignSelf: 'flex-start',
    textTransform: 'capitalize',
    color: COLORS.mintGreen,
    backgroundColor: 'rgba(22, 125, 120, 0.25)',
    borderRadius: 20,
    overflow: 'hidden',
    paddingHorizontal: 10,
    paddingVertical: 5,
    fontWeight: '700',
  },
  filterRow: { flexDirection: 'row', gap: 10 },
  filterColumn: { flexDirection: 'column' },
  filterHalf: { flex: 1 },
  chips: { flexDirection: 'row', flexWrap: 'wrap', gap: 7 },
  chip: {
    borderWidth: 1,
    borderColor: COLORS.glassBorder,
    backgroundColor: COLORS.glassInput,
    paddingHorizontal: 10,
    paddingVertical: 7,
    borderRadius: 20,
  },
  selectedChip: { backgroundColor: COLORS.mintGreen, borderColor: COLORS.mintGreen },
  chipText: { color: COLORS.coolGrey, fontSize: 12 },
  selectedChipText: { color: COLORS.deepNavy, fontWeight: '700' },
  empty: {
    padding: 25,
    alignItems: 'center',
    backgroundColor: COLORS.glassBackground,
    borderRadius: 18,
    borderWidth: 1,
    borderColor: COLORS.glassBorder,
  },
  noticeContainer: { paddingHorizontal: 14 },
  notice: {
    backgroundColor: 'rgba(22, 125, 120, 0.28)',
    borderWidth: 1,
    borderColor: 'rgba(97, 214, 179, 0.4)',
    borderRadius: 12,
    padding: 12,
    gap: 4,
  },
  noticeText: { color: COLORS.offWhite },
  noticeDismiss: { color: COLORS.mintGreen, fontWeight: '700' },
  dialogBackdrop: {
    flex: 1,
    justifyContent: 'center',
    alignItems: 'center',
    padding: 24,
    backgroundColor: 'rgba(4, 15, 24, 0.72)',
  },
  dialogCard: {
    width: '100%',
    maxWidth: 420,
    padding: 20,
    gap: 12,
    borderRadius: 18,
    borderWidth: 1,
    borderColor: COLORS.glassBorder,
    backgroundColor: COLORS.cardSurface,
  },
  dialogTitle: { color: COLORS.white, fontSize: 18, fontWeight: '800' },
  dialogMessage: { color: COLORS.coolGrey, fontSize: 14, lineHeight: 21 },
  dialogActions: { flexDirection: 'row', justifyContent: 'flex-end', flexWrap: 'wrap', gap: 8, marginTop: 4 },
  dialogButton: {
    minHeight: 40,
    justifyContent: 'center',
    alignItems: 'center',
    paddingHorizontal: 15,
    borderRadius: 10,
    backgroundColor: COLORS.forestGreen,
  },
  dialogCancelButton: { backgroundColor: COLORS.glassInput, borderWidth: 1, borderColor: COLORS.glassBorder },
  dialogDestructiveButton: { backgroundColor: COLORS.red },
  dialogButtonText: { color: COLORS.white, fontWeight: '700' },
  dialogCancelText: { color: COLORS.white },

  // Bottom Tab Navigation
  tabs: {
    flexDirection: 'row',
    backgroundColor: 'rgba(16, 42, 67, 0.96)',
    borderTopWidth: 1,
    borderTopColor: COLORS.glassBorder,
    paddingHorizontal: 4,
    paddingTop: 8,
    paddingBottom: 10,
  },
  tab: { flex: 1, minWidth: 0, minHeight: 44, alignItems: 'center', justifyContent: 'center', paddingHorizontal: 2 },
  tabText: { color: COLORS.softGrey, fontSize: 12, fontWeight: '600' },
  tabTextCompact: { fontSize: 10 },
  activeTabText: { color: COLORS.mintGreen, fontWeight: '800' },
  activeTabIndicator: { width: 18, height: 3, borderRadius: 2, backgroundColor: COLORS.mintGreen, marginTop: 3 },
  screenTitle: { fontSize: 25, fontWeight: '800', color: COLORS.white, marginBottom: 2 },
});
