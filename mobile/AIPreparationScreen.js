import { useEffect, useMemo, useState } from 'react';
import {
  ActivityIndicator,
  Pressable,
  ScrollView,
  StyleSheet,
  Text,
  TextInput,
  View,
} from 'react-native';
import { Picker } from '@react-native-picker/picker';
import { COLORS } from './src/constants/colors';
import { generateAiPreparation, getAiPreparationOptions } from './api';

const FEATURES = [
  {
    id: 'mock_test',
    title: 'Generate Mock Test',
    description: 'Create a practice exam from previous-year question papers.',
  },
  {
    id: 'important_questions',
    title: 'Generate Most Important Questions',
    description: 'Find recurring questions and topics from previous-year papers.',
  },
];
const EMPTY_YEARS = [];

function normalizeAnswer(value) {
  return String(value || '').trim().toLocaleLowerCase();
}

function sourceDescription(source) {
  const details = [
    source.subject,
    source.examType,
    String(source.year),
    source.page ? `page ${source.page}` : null,
    source.questionNumber ? `question ${source.questionNumber}` : null,
  ].filter(Boolean);
  return details.join(' · ');
}

function FeatureCard({ feature, selected, onPress, disabled }) {
  return (
    <Pressable
      accessibilityRole="button"
      accessibilityState={{ selected }}
      disabled={disabled}
      onPress={onPress}
      style={[styles.featureCard, selected && styles.selectedFeatureCard, disabled && styles.disabledOption]}
    >
      <Text style={styles.featureTitle}>{feature.title}</Text>
      <Text style={styles.bodyText}>{feature.description}</Text>
    </Pressable>
  );
}

function OptionRow({ selected, onPress, children, disabled }) {
  return (
    <Pressable
      accessibilityRole="button"
      accessibilityState={{ selected }}
      disabled={disabled}
      onPress={onPress}
      style={[styles.optionRow, selected && styles.selectedOptionRow, disabled && styles.disabledOption]}
    >
      <Text style={styles.bodyText}>{children}</Text>
    </Pressable>
  );
}

function MockTestResult({ result }) {
  const [answers, setAnswers] = useState({});
  const [submitted, setSubmitted] = useState(false);
  const [revealed, setRevealed] = useState(false);
  const questions = result.questions || [];
  const objectiveQuestions = questions.filter((question) => question.type === 'multiple_choice');
  const score = objectiveQuestions.reduce(
    (total, question, index) => total
      + (normalizeAnswer(answers[questions.indexOf(question)])
        === normalizeAnswer(question.answer) ? 1 : 0),
    0,
  );
  const showAnswers = submitted || revealed;

  return (
    <View style={styles.resultBlock}>
      <Text style={styles.sectionTitle}>Your mock test</Text>
      <Text style={styles.bodyText}>
        {result.generatedCount} questions · {result.sourcePapersFound} source papers
      </Text>
      {result.message ? <Text style={styles.warningText}>{result.message}</Text> : null}
      {result.missingYears?.length ? (
        <Text style={styles.mutedText}>
          No source papers were found for: {result.missingYears.join(', ')}.
        </Text>
      ) : null}

      {questions.map((question, index) => (
        <View key={`${index}-${question.question}`} style={styles.questionCard}>
          <Text style={styles.questionNumber}>Question {index + 1}</Text>
          <Text style={styles.questionText}>{question.question}</Text>
          <Text style={styles.mutedText}>
            {question.type.replace('_', ' ')}
            {question.marks ? ` · ${question.marks} marks` : ''}
            {question.origin === 'ai_generated' ? ' · AI-generated variation' : ' · Past-paper question'}
          </Text>
          {question.type === 'multiple_choice' ? (
            question.options.map((option, optionIndex) => (
              <OptionRow
                key={`${optionIndex}-${option}`}
                selected={answers[index] === option}
                disabled={submitted}
                onPress={() => setAnswers((current) => ({ ...current, [index]: option }))}
              >
                {option}
              </OptionRow>
            ))
          ) : (
            <TextInput
              editable={!submitted}
              multiline
              onChangeText={(text) => setAnswers((current) => ({ ...current, [index]: text }))}
              placeholder="Write your answer"
              placeholderTextColor={COLORS.softGrey}
              style={[styles.input, styles.answerInput]}
              value={answers[index] || ''}
            />
          )}
          {showAnswers ? (
            <View style={styles.answerBlock}>
              <Text style={styles.answerTitle}>
                {question.type === 'multiple_choice' ? 'Correct answer' : 'Suggested answer'}
              </Text>
              <Text style={styles.bodyText}>{question.answer}</Text>
              {question.explanation ? <Text style={styles.mutedText}>{question.explanation}</Text> : null}
            </View>
          ) : null}
          {question.sources?.length ? (
            <View style={styles.sourceList}>
              {question.sources.map((source, sourceIndex) => (
                <Text key={`${source.paperId}-${sourceIndex}`} style={styles.sourceText}>
                  Source: {sourceDescription(source)}
                </Text>
              ))}
            </View>
          ) : null}
        </View>
      ))}

      {submitted ? (
        <View style={styles.noticeCard}>
          <Text style={styles.sectionTitle}>Objective score: {score}/{objectiveQuestions.length}</Text>
          {questions.length > objectiveQuestions.length ? (
            <Text style={styles.bodyText}>
              Descriptive responses require manual evaluation.
            </Text>
          ) : null}
        </View>
      ) : null}
      <View style={styles.buttonRow}>
        {!submitted ? (
          <Pressable onPress={() => setSubmitted(true)} style={styles.primaryButton}>
            <Text style={styles.primaryButtonText}>Submit test</Text>
          </Pressable>
        ) : null}
        {!showAnswers ? (
          <Pressable onPress={() => setRevealed(true)} style={styles.secondaryButton}>
            <Text style={styles.secondaryButtonText}>Reveal answers</Text>
          </Pressable>
        ) : null}
      </View>
    </View>
  );
}

function ImportantQuestionsResult({ result, onStartMockTest }) {
  return (
    <View style={styles.resultBlock}>
      <Text style={styles.sectionTitle}>Most important questions</Text>
      <Text style={styles.bodyText}>
        Priorities are estimates from the selected historical papers, not a guarantee
        of what will appear in a future examination.
      </Text>
      {result.message ? <Text style={styles.warningText}>{result.message}</Text> : null}
      {result.missingYears?.length ? (
        <Text style={styles.mutedText}>
          No source papers were found for: {result.missingYears.join(', ')}.
        </Text>
      ) : null}
      {result.questions.map((question, index) => (
        <View key={`${index}-${question.question}`} style={styles.questionCard}>
          <View style={styles.priorityRow}>
            <Text style={styles.questionNumber}>Question {index + 1}</Text>
            <Text style={[styles.priority, styles[`priority_${question.priority}`]]}>
              {question.priority} priority
            </Text>
          </View>
          <Text style={styles.questionText}>{question.question}</Text>
          <Text style={styles.bodyText}>{question.priorityReason}</Text>
          {question.marks ? <Text style={styles.mutedText}>Marks: {question.marks}</Text> : null}
          {question.answer ? (
            <View style={styles.answerBlock}>
              <Text style={styles.answerTitle}>Revision answer</Text>
              <Text style={styles.bodyText}>{question.answer}</Text>
              {question.explanation ? <Text style={styles.mutedText}>{question.explanation}</Text> : null}
            </View>
          ) : null}
          {question.sources?.length ? (
            <View style={styles.sourceList}>
              {question.sources.map((source, sourceIndex) => (
                <Text key={`${source.paperId}-${sourceIndex}`} style={styles.sourceText}>
                  Source: {sourceDescription(source)}
                </Text>
              ))}
            </View>
          ) : null}
        </View>
      ))}
      <Pressable onPress={onStartMockTest} style={styles.primaryButton}>
        <Text style={styles.primaryButtonText}>Use these papers to generate a mock test</Text>
      </Pressable>
    </View>
  );
}

export default function AIPreparationScreen({ user, onBack, onSignIn }) {
  const [loadingOptions, setLoadingOptions] = useState(true);
  const [loadingGeneration, setLoadingGeneration] = useState(false);
  const [optionsError, setOptionsError] = useState('');
  const [generationError, setGenerationError] = useState('');
  const [subjects, setSubjects] = useState([]);
  const [feature, setFeature] = useState('');
  const [subjectIndex, setSubjectIndex] = useState('');
  const [startYear, setStartYear] = useState(null);
  const [endYear, setEndYear] = useState(null);
  const [questionCount, setQuestionCount] = useState('10');
  const [hostedConsent, setHostedConsent] = useState(false);
  const [result, setResult] = useState(null);

  useEffect(() => {
    let active = true;
    getAiPreparationOptions()
      .then((response) => {
        if (!active) return;
        const availableSubjects = response.subjects || [];
        setSubjects(availableSubjects);
        if (availableSubjects.length) setSubjectIndex('0');
      })
      .catch((error) => {
        if (active) setOptionsError(error.message || 'Could not load available papers.');
      })
      .finally(() => {
        if (active) setLoadingOptions(false);
      });
    return () => {
      active = false;
    };
  }, []);

  const selectedSubject = subjectIndex === '' ? null : subjects[Number(subjectIndex)];
  const availableYears = selectedSubject?.availableYears || EMPTY_YEARS;
  const validQuestionCount = Number(questionCount);
  const maxQuestions = feature === 'important_questions' ? 20 : 30;
  useEffect(() => {
    if (!availableYears.length) {
      setStartYear(null);
      setEndYear(null);
      return;
    }
    setStartYear((current) => availableYears.includes(current) ? current : availableYears[0]);
    setEndYear((current) => availableYears.includes(current)
      ? current
      : availableYears[availableYears.length - 1]);
  }, [availableYears]);
  const selectedRange = useMemo(() => {
    if (!selectedSubject || startYear == null || endYear == null || startYear > endYear) {
      return { paperCount: 0, missingYears: [] };
    }
    const years = new Set(availableYears);
    const papersByYear = new Map(
      (selectedSubject.papersByYear || []).map((entry) => [entry.year, entry.count]),
    );
    let paperCount = 0;
    for (let year = startYear; year <= endYear; year += 1) {
      paperCount += papersByYear.get(year) || 0;
    }
    return {
      paperCount,
      missingYears: Array.from({ length: endYear - startYear + 1 }, (_, index) => startYear + index)
        .filter((year) => !years.has(year)),
    };
  }, [selectedSubject, availableYears, startYear, endYear]);

  function chooseSubject(value) {
    setSubjectIndex(value);
    const next = subjects[Number(value)];
    const years = next?.availableYears || [];
    setStartYear(years.length ? years[0] : null);
    setEndYear(years.length ? years[years.length - 1] : null);
    setResult(null);
    setGenerationError('');
  }

  function chooseFeature(nextFeature) {
    setFeature(nextFeature);
    setResult(null);
    setGenerationError('');
    const limit = nextFeature === 'important_questions' ? 20 : 30;
    if (Number(questionCount) > limit) setQuestionCount(String(limit));
  }

  async function generate() {
    if (!feature || !selectedSubject || startYear == null || endYear == null) return;
    if (!Number.isInteger(validQuestionCount) || validQuestionCount < 1 || validQuestionCount > maxQuestions) {
      setGenerationError(`Choose between 1 and ${maxQuestions} questions.`);
      return;
    }
    setLoadingGeneration(true);
    setGenerationError('');
    setResult(null);
    try {
      const response = await generateAiPreparation({
        feature,
        subject: selectedSubject.subject,
        subject_code: selectedSubject.subjectCode,
        department_id: selectedSubject.departmentId,
        semester: selectedSubject.semester,
        start_year: startYear,
        end_year: endYear,
        question_count: validQuestionCount,
        hosted_fallback_consent: hostedConsent,
      });
      setResult(response);
    } catch (error) {
      setGenerationError(error.message || 'Generation failed. Please retry.');
    } finally {
      setLoadingGeneration(false);
    }
  }

  function startMockFromImportantQuestions() {
    setFeature('mock_test');
    setQuestionCount(String(Math.min(result?.generatedCount || 10, 30)));
    setResult(null);
    setGenerationError('');
  }

  const canGenerate = Boolean(
    feature
    && selectedSubject
    && startYear != null
    && endYear != null
    && startYear <= endYear
    && selectedRange.paperCount > 0
    && Number.isInteger(validQuestionCount)
    && validQuestionCount >= 1
    && validQuestionCount <= maxQuestions,
  );

  if (!user || user.role !== 'user') {
    return (
      <ScrollView contentContainerStyle={styles.container}>
        <Pressable onPress={onBack} style={styles.backButton}>
          <Text style={styles.backText}>← Back</Text>
        </Pressable>
        <Text style={styles.screenTitle}>AI Exam Preparation</Text>
        <View style={styles.noticeCard}>
          <Text style={styles.bodyText}>
            Sign in with a student reader account to generate practice material from uploaded papers.
          </Text>
          <Pressable onPress={onSignIn} style={styles.primaryButton}>
            <Text style={styles.primaryButtonText}>Go to sign in</Text>
          </Pressable>
        </View>
      </ScrollView>
    );
  }

  return (
    <ScrollView contentContainerStyle={styles.container} keyboardShouldPersistTaps="handled">
      <Pressable onPress={onBack} style={styles.backButton}>
        <Text style={styles.backText}>← Back to Home</Text>
      </Pressable>
      <Text style={styles.screenTitle}>AI Exam Preparation</Text>
      <Text style={styles.bodyText}>
        Questions are generated only from available uploaded question-paper PDFs.
      </Text>

      <Text style={styles.sectionTitle}>1. Choose a feature</Text>
      {FEATURES.map((item) => (
        <FeatureCard
          key={item.id}
          feature={item}
          selected={feature === item.id}
          disabled={loadingGeneration}
          onPress={() => chooseFeature(item.id)}
        />
      ))}

      {!feature ? (
        <View style={styles.noticeCard}>
          <Text style={styles.bodyText}>Choose a feature above to continue.</Text>
        </View>
      ) : loadingOptions ? (
        <View style={styles.loadingRow}>
          <ActivityIndicator color={COLORS.mintGreen} />
          <Text style={styles.bodyText}>Loading subjects and available paper years...</Text>
        </View>
      ) : optionsError ? (
        <View style={styles.noticeCard}>
          <Text style={styles.errorText}>{optionsError}</Text>
          <Pressable
            onPress={() => {
              setLoadingOptions(true);
              setOptionsError('');
              getAiPreparationOptions()
                .then((response) => {
                  const availableSubjects = response.subjects || [];
                  setSubjects(availableSubjects);
                  setSubjectIndex(availableSubjects.length ? '0' : '');
                })
                .catch((error) => setOptionsError(error.message || 'Could not load available papers.'))
                .finally(() => setLoadingOptions(false));
            }}
            style={styles.secondaryButton}
          >
            <Text style={styles.secondaryButtonText}>Retry loading papers</Text>
          </Pressable>
        </View>
      ) : !subjects.length ? (
        <View style={styles.noticeCard}>
          <Text style={styles.sectionTitle}>No eligible papers available</Text>
          <Text style={styles.bodyText}>
            There are no uploaded question-paper PDFs with valid year metadata. Try again after papers are uploaded and their exam years are corrected.
          </Text>
        </View>
      ) : (
        <>
          <Text style={styles.sectionTitle}>2. Select subject and paper scope</Text>
          <Text style={styles.label}>Subject · department · semester</Text>
          <View style={styles.pickerContainer}>
            <Picker
              enabled={!loadingGeneration}
              onValueChange={chooseSubject}
              selectedValue={subjectIndex}
              style={styles.picker}
              dropdownIconColor={COLORS.white}
            >
              {subjects.map((subject, index) => (
                <Picker.Item
                  key={`${subject.departmentId}-${subject.semester}-${subject.subjectCode || subject.subject}-${index}`}
                  label={`${subject.subject}${subject.subjectCode ? ` (${subject.subjectCode})` : ''} · ${subject.departmentName || 'Department'} · Sem ${subject.semester}`}
                  value={String(index)}
                />
              ))}
            </Picker>
          </View>

          {selectedSubject ? (
            <>
              <Text style={styles.label}>Start year</Text>
              <View style={styles.pickerContainer}>
                <Picker
                  selectedValue={startYear}
                  onValueChange={(value) => {
                    setStartYear(Number(value));
                    setResult(null);
                  }}
                  enabled={!loadingGeneration}
                  style={styles.picker}
                  dropdownIconColor={COLORS.white}
                >
                  {availableYears.map((year) => <Picker.Item key={year} label={String(year)} value={year} />)}
                </Picker>
              </View>
              <Text style={styles.label}>End year</Text>
              <View style={styles.pickerContainer}>
                <Picker
                  selectedValue={endYear}
                  onValueChange={(value) => {
                    setEndYear(Number(value));
                    setResult(null);
                  }}
                  enabled={!loadingGeneration}
                  style={styles.picker}
                  dropdownIconColor={COLORS.white}
                >
                  {availableYears.map((year) => <Picker.Item key={year} label={String(year)} value={year} />)}
                </Picker>
              </View>
              <Text style={styles.bodyText}>
                Available years: {availableYears.join(', ')}.
              </Text>
              {selectedRange.missingYears.length ? (
                <Text style={styles.warningText}>
                  No paper exists for these years in the selected range: {selectedRange.missingYears.join(', ')}.
                </Text>
              ) : null}
              {selectedRange.paperCount === 0 ? (
                <Text style={styles.errorText}>
                  No papers are available in this range. Select a range containing uploaded papers.
                </Text>
              ) : null}
            </>
          ) : null}

          <Text style={styles.sectionTitle}>3. Choose question count</Text>
          <Text style={styles.bodyText}>
            Maximum {maxQuestions} questions for this feature.
          </Text>
          <View style={styles.countRow}>
            {[10, 20, 30].filter((count) => count <= maxQuestions).map((count) => (
              <Pressable
                key={count}
                disabled={loadingGeneration}
                onPress={() => {
                  setQuestionCount(String(count));
                  setResult(null);
                }}
                style={[styles.countChip, Number(questionCount) === count && styles.selectedCountChip]}
              >
                <Text style={styles.countChipText}>{count}</Text>
              </Pressable>
            ))}
          </View>
          <Text style={styles.label}>Custom number</Text>
          <TextInput
            editable={!loadingGeneration}
            keyboardType="number-pad"
            maxLength={2}
            onChangeText={(value) => {
              setQuestionCount(value);
              setResult(null);
            }}
            placeholder={`1–${maxQuestions}`}
            placeholderTextColor={COLORS.softGrey}
            style={styles.input}
            value={questionCount}
          />

          <Pressable
            accessibilityRole="checkbox"
            accessibilityState={{ checked: hostedConsent }}
            disabled={loadingGeneration}
            onPress={() => setHostedConsent((value) => !value)}
            style={styles.consentRow}
          >
            <Text style={styles.checkbox}>{hostedConsent ? '☑' : '☐'}</Text>
            <Text style={styles.bodyText}>
              I consent to the selected PDF text being sent to the configured hosted AI provider only if local Ollama is unavailable. Local AI is tried first.
            </Text>
          </Pressable>
          <Text style={styles.mutedText}>
            If neither AI provider is available, generation will fail with a retry message.
          </Text>

          <Text style={styles.sectionTitle}>4. Review</Text>
          <View style={styles.summaryCard}>
            <Text style={styles.summaryText}>
              Feature: {FEATURES.find((item) => item.id === feature)?.title || 'Choose a feature'}
            </Text>
            <Text style={styles.summaryText}>
              Subject: {selectedSubject?.subject || '—'} · {selectedSubject?.departmentName || '—'} · Semester {selectedSubject?.semester || '—'}
            </Text>
            <Text style={styles.summaryText}>
              Year range: {startYear ?? '—'} to {endYear ?? '—'}
            </Text>
            <Text style={styles.summaryText}>Source papers found: {selectedRange.paperCount}</Text>
            <Text style={styles.summaryText}>Questions requested: {questionCount}</Text>
          </View>
          <Pressable
            disabled={!canGenerate || loadingGeneration}
            onPress={generate}
            style={[styles.primaryButton, (!canGenerate || loadingGeneration) && styles.disabledButton]}
          >
            <Text style={styles.primaryButtonText}>Generate</Text>
          </Pressable>
          {loadingGeneration ? (
            <View style={styles.loadingRow}>
              <ActivityIndicator color={COLORS.mintGreen} />
              <Text style={styles.bodyText}>Extracting and analyzing the selected PDFs...</Text>
            </View>
          ) : null}
          {generationError ? <Text style={styles.errorText}>{generationError}</Text> : null}
          {result?.hostedProviderDisclosure ? (
            <Text style={styles.warningText}>{result.hostedProviderDisclosure}</Text>
          ) : null}
          {result?.provider === 'local' ? (
            <Text style={styles.mutedText}>Generated using the local AI provider.</Text>
          ) : null}
          {result ? (
            feature === 'mock_test' ? (
              <MockTestResult result={result} />
            ) : (
              <ImportantQuestionsResult result={result} onStartMockTest={startMockFromImportantQuestions} />
            )
          ) : null}
        </>
      )}
    </ScrollView>
  );
}

const styles = StyleSheet.create({
  container: {
    width: '100%',
    maxWidth: 760,
    alignSelf: 'center',
    paddingHorizontal: 16,
    paddingTop: 16,
    paddingBottom: 32,
    gap: 12,
  },
  backButton: { alignSelf: 'flex-start', paddingVertical: 6, paddingHorizontal: 2 },
  backText: { color: COLORS.mintGreen, fontSize: 15, fontWeight: '700' },
  screenTitle: { color: COLORS.white, fontSize: 26, fontWeight: '800' },
  sectionTitle: { color: COLORS.white, fontSize: 18, fontWeight: '800', marginTop: 8 },
  label: { color: COLORS.coolGrey, fontWeight: '700', marginTop: 6 },
  bodyText: { color: COLORS.coolGrey, fontSize: 14, lineHeight: 21 },
  mutedText: { color: COLORS.softGrey, fontSize: 13, lineHeight: 19 },
  featureCard: {
    borderWidth: 1,
    borderColor: COLORS.glassBorder,
    backgroundColor: COLORS.cardSurface,
    borderRadius: 16,
    padding: 16,
    gap: 6,
  },
  selectedFeatureCard: { borderColor: COLORS.mintGreen, borderWidth: 2 },
  featureTitle: { color: COLORS.white, fontWeight: '800', fontSize: 17 },
  pickerContainer: {
    borderWidth: 1,
    borderColor: COLORS.glassBorder,
    borderRadius: 12,
    backgroundColor: COLORS.glassInput,
    overflow: 'hidden',
  },
  picker: { color: COLORS.white, minHeight: 48 },
  input: {
    borderWidth: 1,
    borderColor: COLORS.glassBorder,
    borderRadius: 12,
    paddingHorizontal: 12,
    paddingVertical: 10,
    color: COLORS.white,
    backgroundColor: COLORS.glassInput,
  },
  answerInput: { minHeight: 82, textAlignVertical: 'top', marginTop: 8 },
  countRow: { flexDirection: 'row', gap: 10 },
  countChip: {
    borderWidth: 1,
    borderColor: COLORS.glassBorder,
    borderRadius: 18,
    paddingVertical: 8,
    paddingHorizontal: 18,
  },
  selectedCountChip: { borderColor: COLORS.mintGreen, backgroundColor: COLORS.forestGreen },
  countChipText: { color: COLORS.white, fontWeight: '700' },
  consentRow: { flexDirection: 'row', alignItems: 'flex-start', gap: 10, paddingVertical: 8 },
  checkbox: { color: COLORS.mintGreen, fontSize: 21, lineHeight: 22 },
  summaryCard: {
    borderRadius: 14,
    backgroundColor: COLORS.cardSurface,
    borderWidth: 1,
    borderColor: COLORS.glassBorder,
    padding: 14,
    gap: 7,
  },
  summaryText: { color: COLORS.white, fontSize: 14 },
  primaryButton: {
    backgroundColor: COLORS.teal,
    borderRadius: 12,
    paddingVertical: 13,
    paddingHorizontal: 16,
    alignItems: 'center',
    marginTop: 4,
  },
  primaryButtonText: { color: COLORS.white, fontWeight: '800', textAlign: 'center' },
  secondaryButton: {
    borderWidth: 1,
    borderColor: COLORS.mintGreen,
    borderRadius: 12,
    paddingVertical: 12,
    paddingHorizontal: 16,
    alignItems: 'center',
  },
  secondaryButtonText: { color: COLORS.mintGreen, fontWeight: '700' },
  disabledButton: { opacity: 0.45 },
  disabledOption: { opacity: 0.55 },
  loadingRow: { flexDirection: 'row', alignItems: 'center', gap: 10, padding: 8 },
  errorText: { color: COLORS.red, fontSize: 14, lineHeight: 20 },
  warningText: { color: '#F6CC76', fontSize: 14, lineHeight: 20 },
  noticeCard: {
    borderRadius: 14,
    backgroundColor: COLORS.cardSurface,
    borderWidth: 1,
    borderColor: COLORS.glassBorder,
    padding: 14,
    gap: 10,
  },
  resultBlock: { gap: 10, marginTop: 8 },
  questionCard: {
    borderRadius: 14,
    backgroundColor: COLORS.cardSurface,
    borderWidth: 1,
    borderColor: COLORS.glassBorder,
    padding: 14,
    gap: 9,
  },
  questionNumber: { color: COLORS.mintGreen, fontWeight: '800' },
  questionText: { color: COLORS.white, fontWeight: '700', fontSize: 16, lineHeight: 23 },
  optionRow: {
    borderWidth: 1,
    borderColor: COLORS.glassBorder,
    borderRadius: 10,
    padding: 11,
  },
  selectedOptionRow: { borderColor: COLORS.mintGreen, backgroundColor: COLORS.forestGreen },
  answerBlock: { borderLeftWidth: 3, borderLeftColor: COLORS.mintGreen, paddingLeft: 10, gap: 5 },
  answerTitle: { color: COLORS.mintGreen, fontWeight: '800' },
  sourceList: { borderTopWidth: 1, borderTopColor: COLORS.glassBorder, paddingTop: 8, gap: 5 },
  sourceText: { color: COLORS.softGrey, fontSize: 12 },
  buttonRow: { flexDirection: 'row', gap: 10, flexWrap: 'wrap' },
  priorityRow: { flexDirection: 'row', alignItems: 'center', justifyContent: 'space-between', gap: 8 },
  priority: { textTransform: 'uppercase', fontSize: 12, fontWeight: '800' },
  priority_high: { color: COLORS.red },
  priority_medium: { color: '#F6CC76' },
  priority_low: { color: COLORS.mintGreen },
});
